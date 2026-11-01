import asyncio
import importlib
import unittest
import _bootstrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from voicelink import Config, MongoDBHandler
from voicelink.lyrics import Lrclib
from voicelink.player import connect_channel
from voicelink.exceptions import VoicelinkException
from voicelink.language import LangHandler
from voicelink.utils import dispatch_message
from voicelink.placeholders import BotPlaceholder
from voicelink.views.controller import InteractiveController, PlayPause
from voicelink.views.playlist import PlaylistView


class RuntimeErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_placeholder_resolves_live_node_pool(self):
        nodes = {"one": SimpleNamespace(_players={1: Mock(), 2: Mock()})}
        with patch("voicelink.pool.NodePool._nodes", nodes):
            placeholder = BotPlaceholder(Mock())
            self.assertEqual(placeholder.players_count(), 2)
            self.assertEqual(placeholder.nodes_count(), 1)

    async def test_pause_uses_existing_localization_key(self):
        Basic = importlib.import_module("cogs.basic").Basic
        player = SimpleNamespace(is_paused=False, is_privileged=Mock(return_value=True), set_pause=AsyncMock())
        response = SimpleNamespace(is_done=Mock(return_value=False))
        ctx = SimpleNamespace(interaction=SimpleNamespace(response=response), guild=SimpleNamespace(voice_client=player), author=Mock(), defer=AsyncMock())
        with patch("cogs.basic.send_localized_message", new=AsyncMock()) as send:
            await Basic.pause.callback(SimpleNamespace(), ctx)
        ctx.defer.assert_awaited_once()
        player.set_pause.assert_awaited_once_with(True, ctx.author)
        send.assert_awaited_once_with(ctx, "player.controls.pause.success", ctx.author)

    async def test_controller_acknowledges_before_button_work(self):
        player = SimpleNamespace(channel=SimpleNamespace(mention="channel"), node=SimpleNamespace(_available=True), is_user_join=Mock(return_value=True), settings={})
        with patch.object(Config(), "controller", {"buttons": []}):
            view = InteractiveController(player)
        response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock())
        interaction = SimpleNamespace(response=response, user=Mock(id=42))
        self.assertTrue(await view.interaction_check(interaction))
        response.defer.assert_awaited_once()
        view.stop()

    async def test_deferred_pause_button_edits_original_message(self):
        player = SimpleNamespace(is_paused=False, current=Mock(), is_privileged=Mock(return_value=True), set_pause=AsyncMock(), pause_votes=set(), resume_votes=set(), settings={})
        button = PlayPause(player=player, btn_data={})
        interaction = SimpleNamespace(user=Mock(), edit_original_response=AsyncMock())
        await button.callback(interaction)
        player.set_pause.assert_awaited_once_with(True, interaction.user)
        interaction.edit_original_response.assert_awaited_once_with(view=None)

    async def test_pause_button_votes_use_existing_localizations(self):
        for paused, action in [(False, "pause"), (True, "resume")]:
            player = SimpleNamespace(is_paused=paused, current=Mock(), is_privileged=Mock(return_value=False), required=Mock(return_value=3), pause_votes=set(), resume_votes=set(), settings={})
            button = PlayPause(player=player, btn_data={})
            interaction = SimpleNamespace(user=Mock())
            with patch("voicelink.views.controller.send_localized_message", new=AsyncMock()) as send:
                await button.callback(interaction)
            self.assertEqual(send.await_args.args[1], f"player.controls.{action}.vote")

    async def test_stale_controller_handles_message_disappearing(self):
        player = SimpleNamespace(channel=None)
        with patch.object(Config(), "controller", {"buttons": []}):
            view = InteractiveController(player)
        error = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10008})
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock(side_effect=error)))
        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(view.is_finished())

    async def test_missing_playlist_export_is_disabled(self):
        bot = SimpleNamespace(get_command=Mock(return_value=None))
        author = SimpleNamespace(id=42, guild=SimpleNamespace(id=42))
        primary = SimpleNamespace(ctx=SimpleNamespace(bot=bot, author=author))
        data = {"id": "200", "name": "Favourite", "type": "playlist", "owner": 42, "perms": {}, "tracks": []}
        view = PlaylistView(primary, data)
        self.assertTrue(view.export.disabled)
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), client=bot)
        await view.export.callback(interaction)
        view.stop()

    async def test_full_voice_channel_is_rejected_without_bypass_permissions(self):
        channel = SimpleNamespace(user_limit=1, members=[Mock()], permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True, administrator=False, move_members=False)), connect=AsyncMock())
        ctx = Mock(spec=discord.Interaction)
        ctx.guild = SimpleNamespace(id=42, me=Mock())
        with patch.object(LangHandler, "get_lang", new=AsyncMock(return_value=["no channel", "no permission"])):
            with self.assertRaises(VoicelinkException):
                await connect_channel(ctx, channel)
        channel.connect.assert_not_awaited()

    async def test_full_voice_channel_allows_move_members_permission(self):
        player = SimpleNamespace(volume=100, is_ipc_connected=False)
        guild = SimpleNamespace(id=42, me=Mock())
        channel = SimpleNamespace(guild=guild, user_limit=1, members=[Mock()], permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True, administrator=False, move_members=True)), connect=AsyncMock(return_value=player))
        ctx = Mock(spec=discord.Interaction)
        ctx.guild = guild
        with patch.object(LangHandler, "get_lang", new=AsyncMock(return_value=["no channel", "no permission"])), patch.object(MongoDBHandler, "get_settings", new=AsyncMock(return_value={})), patch("voicelink.player.Player"):
            self.assertIs(await connect_channel(ctx, channel), player)

    async def test_ephemeral_reply_does_not_inherit_request_channel_expiration(self):
        expirations = []
        async def send(*, delete_after=None, **kwargs):
            expirations.append(delete_after)
        ctx = Mock(spec=discord.Interaction)
        ctx.guild = SimpleNamespace(id=42)
        ctx.channel = SimpleNamespace(id=24)
        ctx.response = SimpleNamespace(is_done=Mock(return_value=False), send_message=send)
        with patch.object(MongoDBHandler, "get_settings", new=AsyncMock(return_value={"music_request_channel": {"text_channel_id": 24}})):
            await dispatch_message(ctx, "message", ephemeral=True)
            await dispatch_message(ctx, "message", ephemeral=False)
        self.assertEqual(expirations, [None, 10])


class LyricsConnectionTests(unittest.IsolatedAsyncioTestCase):
    def session(self, *, status=200, error=None):
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=[{"plainLyrics": "lyrics"}], side_effect=error))
        request = Mock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock(return_value=False)
        session = Mock()
        session.get.return_value = request
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session, request

    async def test_success_releases_response_and_session(self):
        session, request = self.session()
        with patch("voicelink.lyrics.aiohttp.ClientSession", return_value=session):
            self.assertEqual(await Lrclib().get("https://example.com"), [{"plainLyrics": "lyrics"}])
        request.__aexit__.assert_awaited_once()
        session.__aexit__.assert_awaited_once()

    async def test_non_success_releases_response(self):
        session, request = self.session(status=404)
        with patch("voicelink.lyrics.aiohttp.ClientSession", return_value=session):
            self.assertEqual(await Lrclib().get("https://example.com"), [])
        request.__aexit__.assert_awaited_once()

    async def test_cancellation_is_not_swallowed_and_closes_connection(self):
        session, request = self.session(error=asyncio.CancelledError())
        with patch("voicelink.lyrics.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(asyncio.CancelledError):
                await Lrclib().get("https://example.com")
        request.__aexit__.assert_awaited_once()
        session.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
