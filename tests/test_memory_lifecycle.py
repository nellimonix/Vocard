import asyncio
import copy
import gc
import logging
import time
import unittest
import weakref
import _bootstrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from voicelink import Config, MongoDBHandler, Player
from voicelink.cache import BoundedTTLCache
from voicelink.views.controller import InteractiveController


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.cache = BoundedTTLCache(ttl=5, max_entries=3, max_bytes=4096, max_entry_bytes=2048, clock=lambda: self.now)

    def test_expired_entries_are_unavailable_without_periodic_cleanup(self):
        self.cache[1] = {"history": []}
        self.now = 6
        self.assertIsNone(self.cache.get(1))
        self.assertEqual(self.cache.bytes_used, 0)

    def test_expiration_releases_all_expired_records(self):
        for key in range(3):
            self.cache[key] = {"history": [key]}
        self.now = 6
        self.assertEqual(self.cache.expire(), 3)
        self.assertEqual(len(self.cache), 0)
        self.assertEqual(self.cache.bytes_used, 0)

    def test_lru_count_limit_preserves_recently_used_records(self):
        for key in range(3):
            self.cache[key] = {"history": [key]}
        self.cache[0]
        self.cache[3] = {"history": [3]}
        self.assertNotIn(1, self.cache)
        self.assertIn(0, self.cache)
        self.assertEqual(len(self.cache), 3)

    def test_volume_limit_is_enforced_during_sustained_insertions(self):
        cache = BoundedTTLCache(max_entries=10000, max_bytes=8192, max_entry_bytes=4096)
        for key in range(5000):
            cache[key] = {"data": str(key) + "x" * 1000}
            self.assertLessEqual(cache.bytes_used, 8192)
        self.assertLess(len(cache), 10)

    def test_oversized_values_are_not_retained(self):
        value = {"playlist": ["x" * 4096]}
        self.cache[1] = value
        self.assertNotIn(1, self.cache)
        self.assertEqual(self.cache.bytes_used, 0)
        self.assertEqual(len(value["playlist"][0]), 4096)

    def test_growing_records_are_remeasured_on_replacement(self):
        value = {"history": []}
        self.cache[1] = value
        value["history"].append("x" * 4096)
        self.cache[1] = value
        self.assertNotIn(1, self.cache)
        self.assertEqual(self.cache.bytes_used, 0)

    def test_cache_handles_shared_and_cyclic_values(self):
        value = {"history": []}
        value["history"].append(value)
        self.cache[1] = value
        self.assertIs(self.cache[1], value)

    def test_access_extends_expiration(self):
        self.cache[1] = {"history": []}
        self.now = 4
        self.cache[1]
        self.now = 6
        self.assertIn(1, self.cache)
        self.now = 12
        self.assertNotIn(1, self.cache)


class UserCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        MongoDBHandler._lock = asyncio.Lock()
        MongoDBHandler._users_buffer = BoundedTTLCache(ttl=5, max_entries=3, max_bytes=4096, max_entry_bytes=2048, clock=lambda: self.now)
        MongoDBHandler._settings_buffer = {}
        MongoDBHandler._last_access = {}
        self.documents = {42: {"_id": 42, "history": ["first"], "playlist": {"200": {"tracks": ["x" * 6000]}}}}
        self.database = SimpleNamespace(find_one=AsyncMock(side_effect=self.find_one), update_one=AsyncMock(side_effect=self.update_one))
        self.database_patch = patch.object(MongoDBHandler, "_users_db", self.database)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)

    async def find_one(self, query, projection=None):
        user = self.documents.get(query["_id"])
        if user is None:
            return None
        if projection:
            return copy.deepcopy({key: value for key, value in user.items() if key == "_id" or key in projection})
        return copy.deepcopy(user)

    async def update_one(self, query, update, **kwargs):
        user = self.documents.setdefault(query["_id"], copy.deepcopy(update.get("$setOnInsert", {"_id": query["_id"]})))
        for key, value in update.get("$set", {}).items():
            user[key] = copy.deepcopy(value)
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def test_history_query_does_not_fetch_or_cache_playlists(self):
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history"), ["first"])
        self.database.find_one.assert_awaited_once_with({"_id": 42}, {"history": 1})
        self.assertLess(MongoDBHandler._users_buffer.bytes_used, 2048)
        self.assertNotIn("playlist", MongoDBHandler._users_buffer[(42, "history")])
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history"), ["first"])
        self.assertEqual(self.database.find_one.await_count, 1)

    async def test_large_documents_are_returned_without_retaining_them(self):
        result = await MongoDBHandler.get_user(42)
        self.assertEqual(len(result["playlist"]["200"]["tracks"][0]), 6000)
        self.assertEqual(len(MongoDBHandler._users_buffer), 0)

    async def test_history_update_invalidates_projected_cache(self):
        await MongoDBHandler.get_user(42, d_type="history")
        await MongoDBHandler.update_user(42, {"$set": {"history": ["second"]}})
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history"), ["second"])

    async def test_expired_history_is_reloaded(self):
        await MongoDBHandler.get_user(42, d_type="history")
        self.documents[42]["history"] = ["second"]
        self.now = 6
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history"), ["second"])
        self.assertEqual(self.database.find_one.await_count, 2)

    async def test_periodic_cleanup_removes_users_and_settings(self):
        await MongoDBHandler.get_user(42, d_type="history")
        MongoDBHandler._settings_buffer[90] = {"_id": 90}
        MongoDBHandler._last_access[90] = time.time() - 1000
        self.now = 6
        await MongoDBHandler.cleanup_cache()
        self.assertEqual(len(MongoDBHandler._users_buffer), 0)
        self.assertEqual(MongoDBHandler._settings_buffer, {})
        self.assertEqual(MongoDBHandler._last_access, {})

    async def test_force_refresh_does_not_leave_stale_parts(self):
        await MongoDBHandler.get_user(42, d_type="history")
        self.documents[42]["history"] = ["second"]
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history", force_refresh=True), ["second"])

    async def test_mutating_results_does_not_modify_cached_history(self):
        result = await MongoDBHandler.get_user(42, d_type="history")
        result.append("local")
        self.assertEqual(await MongoDBHandler.get_user(42, d_type="history"), ["first"])

    async def test_user_access_does_not_accumulate_settings_timestamps(self):
        await MongoDBHandler.get_user(42, d_type="history")
        self.assertEqual(MongoDBHandler._last_access, {})

    async def test_update_cannot_grow_a_record_past_cache_budget(self):
        self.documents[42] = {"_id": 42, "history": ["first"]}
        await MongoDBHandler.get_user(42)
        await MongoDBHandler.update_user(42, {"$set": {"history": ["x" * 6000]}})
        self.assertEqual(len(MongoDBHandler._users_buffer), 0)
        self.assertEqual(MongoDBHandler._users_buffer.bytes_used, 0)
        self.assertEqual(len((await MongoDBHandler.get_user(42))["history"][0]), 6000)

    async def test_bulk_queries_do_not_bypass_cache_limits(self):
        users = [{"_id": index, "history": [str(index)]} for index in range(100)]
        cursor = SimpleNamespace(to_list=AsyncMock(return_value=users))
        cursor.skip = Mock(return_value=cursor)
        database = SimpleNamespace(find=Mock(return_value=cursor))
        with patch.object(MongoDBHandler, "_users_db", database):
            result = await MongoDBHandler.get_users_by_criteria({})
        self.assertEqual(len(result), 100)
        self.assertLessEqual(len(MongoDBHandler._users_buffer), 3)
        self.assertLessEqual(MongoDBHandler._users_buffer.bytes_used, 4096)


class StoredMessage:
    def __init__(self, client, message_id):
        self.client = client
        self.id = message_id
        self.before_edit = None

    async def edit(self, *, view=None, **kwargs):
        if self.before_edit:
            await self.before_edit()
        store = self.client._connection._view_store
        store.remove_message_tracking(self.id)
        if view is not None:
            store.add_view(view, self.id)
        return self

    async def delete(self):
        self.client._connection.parse_message_delete({"id": str(self.id), "channel_id": "42"})


class ControllerPlayer:
    invoke_controller = Player.invoke_controller
    teardown = Player.teardown
    _stop_controller_view = Player._stop_controller_view

    def __init__(self):
        self.settings = {}
        self._controller_view = None
        self._closing = False
        self._updating = False
        self.channel = SimpleNamespace(id=42)
        self.guild = SimpleNamespace(id=42, name="test")
        self.controller = None
        self.current = None
        self.context = Mock()
        self._logger = logging.getLogger("controller-test")
        self.is_position_fresh = AsyncMock(return_value=True)
        self.update_voice_status = AsyncMock()
        self._cancel_inactive_cleanup_timer = Mock()
        self.destroy = AsyncMock()
        self.is_ipc_connected = False
        self.joinTime = time.time()

    def build_embed(self, *args):
        return discord.Embed()


class ControllerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        await self.client._async_setup_hook()
        self.store = self.client._connection._view_store
        self.next_id = 1000
        self.config_patch = patch.object(Config(), "controller", {"buttons": [{"stop": {}}]})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.sender_patch = patch("voicelink.player.dispatch_message", new=self.send)
        self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)
        self.settings_patch = patch.object(MongoDBHandler, "update_settings", new=AsyncMock())
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    async def asyncTearDown(self):
        for view in list(self.store._synced_message_views.values()):
            view.stop()
        await self.client.close()

    async def send(self, ctx, *, view, **kwargs):
        self.next_id += 1
        message = StoredMessage(self.client, self.next_id)
        self.store.add_view(view, message.id)
        return message

    async def test_repeated_same_message_updates_do_not_accumulate_handlers(self):
        player = ControllerPlayer()
        views = []
        for _ in range(1000):
            await player.invoke_controller()
            views.append(weakref.ref(player._controller_view))
        gc.collect()
        self.assertEqual(len(self.store._views), 1)
        self.assertEqual(sum(len(items) for items in self.store._views.values()), 1)
        self.assertEqual(sum(ref() is not None for ref in views), 1)

    async def test_deleted_message_replacement_stops_old_controller(self):
        player = ControllerPlayer()
        await player.invoke_controller()
        old_view = weakref.ref(player._controller_view)
        player.is_position_fresh.return_value = False
        await player.invoke_controller()
        gc.collect()
        self.assertIsNone(old_view())
        self.assertEqual(len(self.store._views), 1)

    async def test_teardown_releases_players_even_after_message_deletion(self):
        players = []
        for _ in range(100):
            player = ControllerPlayer()
            await player.invoke_controller()
            players.append(weakref.ref(player))
            await player.teardown()
        del player
        gc.collect()
        self.assertEqual(self.store._views, {})
        self.assertEqual(sum(ref() is not None for ref in players), 0)

    async def test_refresh_racing_with_teardown_does_not_restore_view(self):
        player = ControllerPlayer()
        await player.invoke_controller()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        player.controller.before_edit = wait
        refresh = asyncio.create_task(player.invoke_controller())
        await entered.wait()
        await player.teardown()
        release.set()
        await refresh
        self.assertEqual(self.store._views, {})
        self.assertIsNone(player._controller_view)

    async def test_forbidden_refresh_does_not_leave_registered_view(self):
        player = ControllerPlayer()
        await player.invoke_controller()
        async def forbidden():
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013})
        player.controller.before_edit = forbidden
        await player.invoke_controller()
        self.assertEqual(self.store._views, {})
        self.assertIsNone(player._controller_view)

    async def test_cancelled_refresh_stops_new_view(self):
        player = ControllerPlayer()
        await player.invoke_controller()
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        player.controller.before_edit = wait
        refresh = asyncio.create_task(player.invoke_controller())
        await entered.wait()
        refresh.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await refresh
        self.assertEqual(self.store._views, {})
        self.assertFalse(player._updating)

    async def test_stale_controller_is_disabled_without_channel_access(self):
        player = ControllerPlayer()
        view = InteractiveController(player)
        self.store.add_view(view, 9999)
        player.channel = None
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.edit_message.assert_awaited_once_with(view=None)
        self.assertEqual(self.store._views, {})


if __name__ == "__main__":
    unittest.main()
