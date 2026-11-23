import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import _bootstrap
import discord
from discord.http import handle_message_parameters

from voicelink import Config, Player
from voicelink.enums import LoopType
from voicelink.views.compact_controller import CompactController, ICON_ROWS, ICONS
from voicelink.views.controller import ADD_TRACK_ID, Stop, StopPlayback


def sample_player(*, custom=True):
    emoji_ids = {item[1] for item in ICONS.values()} if custom else set()
    track = SimpleNamespace(title="Testing", author="Artist", uri="https://music.yandex.com/track/1",
                            thumbnail="https://avatars.yandex.net/image", source="yandexmusic",
                            requester=None, length=180000, is_stream=False, is_seekable=True)
    queue = SimpleNamespace(count=2, is_empty=False, history=lambda: [],
                            _repeat=SimpleNamespace(peek_next=lambda: LoopType.OFF))
    return SimpleNamespace(bot=SimpleNamespace(compact_app_emojis=emoji_ids), current=track, position=90000,
                           is_paused=False, volume=100, queue=queue, get_msg=lambda key: key)


class CompactControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_message_uses_thumbnail_and_uploaded_emoji_ids(self):
        player = sample_player()
        view = CompactController(player, discord.Embed(colour=0x896657))
        with handle_message_parameters(view=view, embed=None, content=None) as params:
            self.assertEqual(params.payload["flags"] & 32768, 32768)
            container, = params.payload["components"]
        self.assertEqual(container["type"], 17)
        items = container["components"]
        self.assertEqual(items[0]["accessory"]["type"], 11)
        self.assertNotIn(12, [node["type"] for node in items])
        rows = [node["components"] for node in items if node["type"] == 1]
        self.assertEqual([len(row) for row in rows], [len(row) for row in ICON_ROWS])
        self.assertTrue(all(not button.get("label") for row in rows for button in row))
        self.assertEqual(int(rows[0][2]["emoji"]["id"]), ICONS["pause"][1])
        self.assertIsInstance(view.frame.children[-1].children[-2], StopPlayback)
        self.assertIsInstance(view.frame.children[-1].children[-1], Stop)
        self.assertEqual(view.frame.children[-1].children[-3].emoji.id, ICONS["lyrics"][1])
        self.assertIn("01:30", view.readout.content)
        player.volume = 60
        view.refresh_readout(120000)
        self.assertIn("02:00", view.readout.content)
        self.assertIn("60%", view.readout.content)

    async def test_non_owner_app_falls_back_to_unicode(self):
        player = sample_player(custom=False)
        player.is_paused = True
        view = CompactController(player, discord.Embed())
        row = next(item for item in view.frame.children if isinstance(item, discord.ui.ActionRow))
        self.assertEqual(row.children[2].emoji.name, ICONS["play"][2])

    async def test_add_button_can_open_a_modal_without_prior_defer(self):
        player = sample_player()
        player.channel = object()
        player.node = SimpleNamespace(_available=True)
        view = CompactController(player, discord.Embed())
        interaction = SimpleNamespace(data={"custom_id": ADD_TRACK_ID}, user=SimpleNamespace(id=55),
                                      response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock()))
        with patch.object(Config(), "bot_access_user", [55]):
            self.assertTrue(await view.interaction_check(interaction))
        interaction.response.defer.assert_not_awaited()

    async def test_explicit_stop_holds_playback_without_a_disconnect(self):
        from cogs.listeners import Listeners

        player = SimpleNamespace(current=object(), stop=AsyncMock(), invoke_controller=AsyncMock(),
                                 update_voice_status=AsyncMock(), do_next=AsyncMock(), playback_stopped=False)
        await Player.stop_playback(player)
        player.stop.assert_awaited_once_with(unpause=True)
        self.assertTrue(player.playback_stopped)
        await Listeners.on_voicelink_track_end(None, player, object(), "stopped")
        player.do_next.assert_not_awaited()
