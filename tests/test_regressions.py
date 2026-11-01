import asyncio
import copy
import importlib
import logging
import unittest
import _bootstrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from voicelink import Config, LangHandler, MongoDBHandler, Player, SearchType, TrackLoadError
from voicelink.enums import RequestMethod
from voicelink.cache import BoundedTTLCache
from voicelink.player import connect_channel
from voicelink.utils import dispatch_message

TEST_CONFIG = {"client_id": 1, "prefix": "!", "logging": {"file": {"enable": False}}, "ipc_client": {"enable": False}}
Config(TEST_CONFIG)
LangHandler.init()
with patch("function.open_json", return_value=TEST_CONFIG):
    main = importlib.import_module("main")

from cogs.basic import Basic


def track(index, uri=None):
    return SimpleNamespace(author="Artist", title=f"Track {index}", formatted_length="03:00", uri=uri or f"https://example.com/{index}")


class AutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Basic(self.bot)
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=10))

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_search_caps_choices_and_rejects_long_values(self):
        node = SimpleNamespace(get_tracks=AsyncMock(return_value=[track(0, "https://example.com/"+"x"*100)] + [track(i) for i in range(40)]))
        with patch("voicelink.NodePool.get_node", return_value=node):
            choices = await self.cog.play_autocomplete(self.interaction, "query")
        self.assertEqual(len(choices), 25)
        self.assertTrue(all(len(choice.value) <= 100 and len(choice.name) <= 100 for choice in choices))
        self.assertEqual(choices[0].value, "https://example.com/0")

    async def test_source_failure_returns_no_choices(self):
        node = SimpleNamespace(get_tracks=AsyncMock(side_effect=TrackLoadError("unavailable")))
        with patch("voicelink.NodePool.get_node", return_value=node):
            self.assertEqual(await self.cog.play_autocomplete(self.interaction, "query"), [])

    async def test_timeout_returns_no_choices(self):
        node = SimpleNamespace(get_tracks=AsyncMock(side_effect=asyncio.TimeoutError))
        with patch("voicelink.NodePool.get_node", return_value=node):
            self.assertEqual(await self.cog.play_autocomplete(self.interaction, "query"), [])

    async def test_context_play_acknowledges_before_connecting(self):
        response = SimpleNamespace(defer=AsyncMock())
        interaction = SimpleNamespace(response=response, guild=SimpleNamespace(voice_client=None), user=SimpleNamespace(mention="user"))
        player = SimpleNamespace(is_user_join=Mock(return_value=True), get_tracks=AsyncMock(return_value=[]))
        async def connect(ctx):
            response.defer.assert_awaited_once()
            return player
        with patch("voicelink.connect_channel", side_effect=connect), patch("cogs.basic.send_localized_message", new=AsyncMock()):
            await self.cog._play(interaction, SimpleNamespace(content="https://example.com/track", attachments=[]))


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_voice_updates_are_skipped(self):
        player = SimpleNamespace(channel=SimpleNamespace(id=42), send=AsyncMock(), _logger=logging.getLogger("test"))
        for state in [{}, {"sessionId": "session"}, {"sessionId": "session", "event": {"token": "token", "endpoint": None}}]:
            player._voice_state = state
            await Player._dispatch_voice_update(player)
        player.send.assert_not_awaited()

    async def test_valid_voice_update_accepts_additional_state_fields(self):
        player = SimpleNamespace(channel=SimpleNamespace(id=42), _voice_state={"sessionId": "session", "event": {"token": "token", "endpoint": "voice.example"}, "extra": True}, send=AsyncMock(), _logger=logging.getLogger("test"))
        await Player._dispatch_voice_update(player)
        player.send.assert_awaited_once_with(method=RequestMethod.PATCH, data={"voice": {"token": "token", "endpoint": "voice.example", "sessionId": "session", "channelId": "42"}})

    async def test_voice_event_order_does_not_block_connection(self):
        for server_first in [True, False]:
            channel = SimpleNamespace(id=42)
            player = SimpleNamespace(channel=channel, guild=SimpleNamespace(get_channel=Mock(return_value=channel)), _voice_state={}, send=AsyncMock(), _logger=logging.getLogger("test"))
            player._dispatch_voice_update = lambda state=None: Player._dispatch_voice_update(player, state)
            updates = [(Player.on_voice_server_update, {"token": "token", "endpoint": "voice.example"}), (Player.on_voice_state_update, {"session_id": "session", "channel_id": "42"})]
            for method, payload in updates if server_first else reversed(updates):
                await method(player, payload)
            player.send.assert_awaited_once()

    async def test_interaction_connect_uses_client_and_explicit_channel(self):
        interaction = Mock(spec=discord.Interaction)
        interaction.client = Mock()
        interaction.guild = SimpleNamespace(id=42, me=Mock())
        interaction.user = SimpleNamespace(voice=None)
        player = SimpleNamespace(volume=100, is_ipc_connected=False)
        channel = SimpleNamespace(guild=interaction.guild, user_limit=0, permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True)), connect=AsyncMock(return_value=player))
        with patch("voicelink.player.Player") as factory, patch.object(LangHandler, "get_lang", new=AsyncMock(return_value=["no channel", "no permission"])), patch.object(MongoDBHandler, "get_settings", new=AsyncMock(return_value={})):
            self.assertIs(await connect_channel(interaction, channel), player)
            self.assertIs(factory.call_args.args[0], interaction.client)

    async def test_voice_status_permission_errors_are_nonfatal(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        channel = SimpleNamespace(type=discord.ChannelType.voice, edit=AsyncMock(side_effect=discord.Forbidden(response, {"code": 50013, "message": "Missing Permissions"})))
        player = SimpleNamespace(channel=channel, settings={"stage_announce_template": "Playing"}, _ph=SimpleNamespace(variables={}, replace=Mock(return_value="Playing")), _logger=Mock())
        await Player.update_voice_status(player)
        player._logger.error.assert_not_called()

    async def test_deleted_controller_is_recreated(self):
        response = SimpleNamespace(status=404, reason="Not Found")
        player = SimpleNamespace(settings={}, _updating=False, channel=Mock(), build_embed=Mock(return_value=discord.Embed()), current=None, controller=SimpleNamespace(edit=AsyncMock(side_effect=discord.NotFound(response, {"code": 10008, "message": "Unknown Message"}))), is_position_fresh=AsyncMock(return_value=True), context=Mock(), _logger=Mock())
        player._closing = False
        player._controller_view = None
        player._stop_controller_view = lambda: Player._stop_controller_view(player)
        replacement = Mock()
        with patch("voicelink.player.InteractiveController", return_value=Mock()), patch("voicelink.player.dispatch_message", new=AsyncMock(return_value=replacement)):
            await Player.invoke_controller(player)
        self.assertIs(player.controller, replacement)
        self.assertFalse(player._updating)
        player._logger.error.assert_not_called()

    async def test_inactive_cleanup_ignores_bots_and_deafened_members(self):
        members = [SimpleNamespace(bot=True, voice=SimpleNamespace(self_deaf=False)), SimpleNamespace(bot=False, voice=SimpleNamespace(self_deaf=True))]
        player = SimpleNamespace(_guild=SimpleNamespace(id=1), _node=SimpleNamespace(_players={1: Mock()}), channel=SimpleNamespace(members=members), is_playing=True, queue=SimpleNamespace(is_empty=False), settings={}, teardown=AsyncMock(), _logger=Mock())
        await Player._inactive_cleanup_timer_worker(player, 0)
        player.teardown.assert_awaited_once()


class MongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        MongoDBHandler._lock = asyncio.Lock()
        MongoDBHandler._users_buffer = BoundedTTLCache()
        MongoDBHandler._settings_buffer = {}
        MongoDBHandler._last_access = {}

    async def test_new_user_uses_atomic_upsert(self):
        document = copy.deepcopy(MongoDBHandler._user_base)
        document["_id"] = 42
        database = SimpleNamespace(find_one=AsyncMock(side_effect=[None, document]), update_one=AsyncMock())
        with patch.object(MongoDBHandler, "_users_db", database):
            self.assertEqual((await MongoDBHandler.get_user(42))["_id"], 42)
        self.assertTrue(database.update_one.call_args.kwargs["upsert"])
        self.assertIn("$setOnInsert", database.update_one.call_args.args[1])

    async def test_new_settings_use_atomic_upsert(self):
        database = SimpleNamespace(find_one=AsyncMock(side_effect=[None, {"_id": 42}]), update_one=AsyncMock())
        with patch.object(MongoDBHandler, "_settings_db", database):
            self.assertEqual((await MongoDBHandler.get_settings(42))["_id"], 42)
        self.assertTrue(database.update_one.call_args.kwargs["upsert"])

    async def test_unchanged_upsert_does_not_insert_duplicate(self):
        MongoDBHandler._users_buffer[42] = {"_id": 42, "history": []}
        database = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1, modified_count=0)), insert_one=AsyncMock())
        with patch.object(MongoDBHandler, "_users_db", database):
            self.assertTrue(await MongoDBHandler.update_user(42, {"$set": {"history": []}}, upsert=True))
        database.insert_one.assert_not_awaited()


class MessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_webhook_is_fetched_through_channel(self):
        message = Mock(spec=discord.WebhookMessage)
        message.id = 42
        channel_message = Mock(spec=discord.Message)
        interaction = Mock(spec=discord.Interaction)
        interaction.response = SimpleNamespace(is_done=Mock(return_value=True))
        interaction.followup = SimpleNamespace(send=AsyncMock(return_value=message))
        interaction.channel = SimpleNamespace(id=1, fetch_message=AsyncMock(return_value=channel_message))
        interaction.guild = SimpleNamespace(id=2)
        with patch.object(MongoDBHandler, "get_settings", new=AsyncMock(return_value={})):
            self.assertIs(await dispatch_message(interaction, "playing", requires_fetch=True), channel_message)
        interaction.channel.fetch_message.assert_awaited_once_with(42)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_cogs_and_localized_commands_register(self):
        bot = main.Vocard(command_prefix=main.get_prefix, intents=main.intents, tree_cls=main.CommandCheck, help_command=None)
        await bot._async_setup_hook()
        bot.ipc_client = SimpleNamespace(_is_connected=False)
        with patch("cogs.listeners.Listeners.start_nodes", new=AsyncMock()), patch("cogs.listeners.Listeners.restore_last_session_players", new=AsyncMock()):
            for module in ["basic", "effect", "playlist", "settings", "listeners", "task"]:
                await bot.load_extension("cogs."+module)
        try:
            self.assertEqual(len(bot.cogs), 6)
            self.assertIsNotNone(bot.get_command("playlist permission"))
            self.assertIsNotNone(bot.get_command("settings reset"))
            self.assertIsNone(bot.get_command("settings prefix"))
            self.assertIsNone(bot.get_command("search"))
            self.assertIsNone(bot.get_command("playtop"))
            translator = main.Translator()
            for command in bot.tree.get_commands():
                payload = await command.get_translated_payload(bot.tree, translator)
                self.check_payload(payload)
        finally:
            for name in list(bot.cogs): await bot.remove_cog(name)
            await bot.close()

    def check_payload(self, payload):
        if payload.get("description"):
            self.assertLessEqual(len(payload["description"]), 100)
            self.assertNotRegex(payload["description"], r'^(basic|effect|playlist|settings)\.')
        for description in payload.get("description_localizations", {}).values():
            self.assertLessEqual(len(description), 100)
        for option in payload.get("options", []): self.check_payload(option)

    async def test_intents_and_default_search_preserve_fork_behavior(self):
        self.assertFalse(main.bot.intents.message_content)
        self.assertFalse(main.bot.intents.members)
        self.assertFalse(main.bot.intents.presences)
        self.assertTrue(main.bot.intents.voice_states)
        self.assertIs(Config().search_platform, SearchType.YANDEX_MUSIC)

    async def test_russian_translation_and_english_fallback(self):
        translator = main.Translator()
        string = discord.app_commands.locale_str("Play", key="basic.play")
        self.assertEqual(await translator.translate(string, discord.Locale.russian, None), LangHandler._local_langs["RU"]["basic.play"])
        self.assertEqual(await translator.translate(string, discord.Locale.german, None), LangHandler._local_langs["EN-US"]["basic.play"])
        self.assertEqual(LangHandler._default_lang, "RU")

    async def test_failed_command_sync_keeps_version_unsynced(self):
        bot = main.Vocard(command_prefix=main.get_prefix, intents=main.intents, tree_cls=main.CommandCheck, help_command=None)
        await bot._async_setup_hook()
        bot.load_extension = AsyncMock()
        bot.tree.sync = AsyncMock(side_effect=RuntimeError("sync failed"))
        with patch.object(MongoDBHandler, "init", new=AsyncMock()), patch("main.IPCClient"), patch("function.open_json", return_value=TEST_CONFIG), patch("function.update_json") as save:
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                await bot.setup_hook()
            save.assert_not_called()
        await bot.close()


if __name__ == "__main__":
    unittest.main()
