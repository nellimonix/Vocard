import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import _bootstrap
import discord
from discord.http import handle_message_parameters

from voicelink import Player
from voicelink.enums import LoopType
from voicelink.views.compact_controller import CompactController, CompactIdle, ICONS, READOUT_ICONS
from voicelink.views.controller import Forward, Rewind, Stop, StopPlayback


def sample_player(*, custom=True):
    emoji_ids = ({name: emoji_id for name, emoji_id, _ in ICONS.values()} | READOUT_ICONS) if custom else {}
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
        self.assertIsNone(container.get("accent_color"))
        items = container["components"]
        self.assertEqual(items[0]["accessory"]["type"], 11)
        self.assertNotIn(12, [node["type"] for node in items])
        self.assertEqual([item["type"] for item in items[1:4]], [10, 14, 10])
        self.assertFalse(items[2]["divider"])
        rows = [node["components"] for node in items if node["type"] == 1]
        self.assertEqual([len(row) for row in rows], [5, 5, 5])
        roles = (("shuffle", "volumedown", None, "volumeup", "loop"),
                 ("rewind", "back", "pause", "skip", "forward"),
                 ("add-track", "queue", "lyrics", "stop", "leave"))
        for row, actions in zip(rows, roles):
            for button, action in zip(row, actions):
                if action:
                    self.assertFalse(button.get("label"))
                    self.assertEqual(int(button["emoji"]["id"]), ICONS[action][1])
        self.assertEqual(rows[0][2]["label"], "100%")
        self.assertTrue(rows[0][2]["disabled"])
        self.assertEqual(rows[2][0]["style"], discord.ButtonStyle.success.value)
        self.assertTrue(rows[2][0]["disabled"])
        self.assertEqual(rows[2][4]["style"], discord.ButtonStyle.danger.value)
        self.assertEqual(int(rows[1][4]["emoji"]["id"]), 1574295953220042752)
        self.assertIsInstance(view.frame.children[-1].children[-2], StopPlayback)
        self.assertIsInstance(view.frame.children[-1].children[-1], Stop)
        self.assertEqual(view.frame.children[-1].children[-3].emoji.id, ICONS["lyrics"][1])
        self.assertIn("01:30", view.progress.content)
        player.volume = 60
        view.refresh_readout(120000)
        self.assertIn("02:00", view.progress.content)
        self.assertNotIn("60%", view.metadata.content)
        self.assertEqual(view.volume_readout.label, "60%")

    async def test_non_owner_app_falls_back_to_unicode(self):
        player = sample_player(custom=False)
        player.is_paused = True
        view = CompactController(player, discord.Embed())
        rows = [item for item in view.frame.children if isinstance(item, discord.ui.ActionRow)]
        self.assertEqual(rows[1].children[2].emoji.name, ICONS["play"][2])
        self.assertIn("▰▰▰▰▰▱▱▱▱▱", view.progress.content)

    async def test_idle_container_does_not_inherit_the_accent_color(self):
        view = CompactIdle(discord.Embed(colour=0xFFDA38, description="Idle"), "Idle")
        self.assertIsNone(view.to_components()[0].get("accent_color"))

    async def test_queue_source_and_requester_share_the_metadata_line(self):
        player = sample_player()
        player.current.requester = SimpleNamespace(mention="<@123456>")
        player.bot.compact_app_emojis["ym_yamusic"] = 1234567
        view = CompactController(player, discord.Embed())
        for size in (0, 1, 2, 10):
            player.queue.count = size
            metadata = view.metadata_text()
            self.assertEqual(metadata, f"-# <:ym_yamusic:1234567> Яндекс Музыка  ·  {size} в очереди  ·  Добавлен: <@123456>")

    async def test_progress_bar_uses_custom_start_middle_and_end_segments(self):
        player = sample_player()
        view = CompactController(player, discord.Embed())

        def icon(name):
            return f"<:{name}:{READOUT_ICONS[name]}>"

        for elapsed, expected in (
            (0, icon("ym_start_0_v2") + icon("ym_mid_0_v2") * 8 + icon("ym_end_0_v2")),
            (90000, icon("ym_start_100_v2") + icon("ym_mid_100_v2") * 4 + icon("ym_mid_0_v2") * 4 + icon("ym_end_0_v2")),
            (180000, icon("ym_start_100_v2") + icon("ym_mid_100_v2") * 8 + icon("ym_end_100_v2")),
        ):
            self.assertIn(expected, view.progress_text(player.current, elapsed))

    async def test_forward_uses_the_emoji_registered_in_the_current_application(self):
        player = sample_player()
        player.bot.compact_app_emojis[ICONS["forward"][0]] = 1574295953220042753
        view = CompactController(player, discord.Embed())
        rows = [item for item in view.frame.children if isinstance(item, discord.ui.ActionRow)]
        self.assertEqual(rows[1].children[4].emoji.id, 1574295953220042753)

    async def test_exported_emoji_takes_priority_over_the_original_when_registered(self):
        player = sample_player()
        player.bot.compact_app_emojis["ym_forward_next30_x15"] = 1574295953220042753
        player.bot.compact_app_emojis["ym_forward_next30_x125"] = 1574295953220042754
        player.bot.compact_app_emojis["ym_pause_x125"] = 1574295953220042755
        view = CompactController(player, discord.Embed())
        rows = [item for item in view.frame.children if isinstance(item, discord.ui.ActionRow)]
        self.assertEqual(rows[1].children[4].emoji.name, "ym_forward_next30_x125")
        self.assertEqual(rows[1].children[4].emoji.id, 1574295953220042754)
        self.assertEqual(rows[1].children[2].emoji.id, 1574295953220042755)

    async def test_compact_seek_intervals_and_track_bounds(self):
        player = sample_player()
        player.is_privileged = Mock(return_value=True)
        player.seek = AsyncMock()
        interaction = SimpleNamespace(user=object(), edit_original_response=AsyncMock())
        forward = Forward(player=player, btn_data={"emoji": ICONS["forward"][2]}, icon_only=True)
        rewind = Rewind(player=player, btn_data={"emoji": ICONS["rewind"][2]}, icon_only=True)
        await forward.callback(interaction)
        await rewind.callback(interaction)
        player.position = 175000
        await forward.callback(interaction)
        player.position = 5000
        await rewind.callback(interaction)
        self.assertEqual(player.seek.await_args_list, [call(120000), call(80000), call(180000), call(0)])

    async def test_explicit_stop_holds_playback_without_a_disconnect(self):
        from cogs.listeners import Listeners

        queue = SimpleNamespace(is_empty=False, clear=Mock())
        player = SimpleNamespace(current=object(), queue=queue, stop=AsyncMock(), invoke_controller=AsyncMock(),
                                 update_voice_status=AsyncMock(), do_next=AsyncMock(), playback_stopped=False)
        await Player.stop_playback(player)
        player.stop.assert_awaited_once_with(unpause=True)
        player.queue.clear.assert_called_once_with()
        self.assertTrue(player.playback_stopped)
        await Listeners.on_voicelink_track_end(None, player, object(), "stopped")
        player.do_next.assert_not_awaited()
