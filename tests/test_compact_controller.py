import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import _bootstrap
import discord
from discord.http import handle_message_parameters

from voicelink import Node, Player, SearchType
from voicelink.enums import LoopType
from voicelink.views.compact_controller import AddTrackModal, CompactController, CompactIdle, CompactQueueEnd, ICONS, READOUT_ICONS
from voicelink.queue import LoopTypeCycle
from voicelink.views.controller import Forward, Rewind, Skip, Stop, StopPlayback


def sample_player(*, custom=True):
    emoji_ids = ({name: emoji_id for name, emoji_id, _ in ICONS.values()} | READOUT_ICONS) if custom else {}
    track = SimpleNamespace(title="Testing", author="Artist", uri="https://music.yandex.com/track/1",
                            thumbnail="https://avatars.yandex.net/image", source="yandexmusic",
                            requester=None, length=180000, is_stream=False, is_seekable=True, artist_url=None)
    queue = SimpleNamespace(count=2, is_empty=False, history=lambda **_: [], _repeat=LoopTypeCycle())
    return SimpleNamespace(bot=SimpleNamespace(compact_app_emojis=emoji_ids), current=track, position=90000,
                           is_paused=False, volume=100, queue=queue, get_msg=lambda key: key)


class CompactControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_messages_setting_applies_to_the_existing_skip_button(self):
        from cogs.settings import Settings

        player = sample_player()
        player.settings = {}
        player.is_playing = True
        player.is_privileged = Mock(return_value=True)
        player.stop = AsyncMock()
        guild = SimpleNamespace(id=42, voice_client=player)
        ctx = SimpleNamespace(guild=guild)
        interaction = SimpleNamespace(user=object())
        button = Skip(player=player, btn_data={"emoji": "⏭️"}, icon_only=True)

        with patch("cogs.settings.MongoDBHandler.get_settings", new_callable=AsyncMock, return_value={}), \
             patch("cogs.settings.MongoDBHandler.update_settings", new_callable=AsyncMock) as update, \
             patch("cogs.settings.LangHandler.get_lang", new_callable=AsyncMock, return_value="Disabled"), \
             patch("cogs.settings.send_localized_message", new_callable=AsyncMock), \
             patch("voicelink.views.controller.send_localized_message", new_callable=AsyncMock) as send:
            await Settings.controllermsg.callback(None, ctx)
            self.assertFalse(player.settings["controller_msg"])
            await button.callback(interaction)
            player.stop.assert_awaited_once_with()
            send.assert_not_awaited()

            await Settings.reset_setting.callback(None, ctx, setting="Controller messages")
            self.assertNotIn("controller_msg", player.settings)
            await button.callback(interaction)

        self.assertEqual(player.stop.await_count, 2)
        self.assertEqual(send.await_args.args[1], "player.controls.skip.success")
        self.assertEqual(update.await_args_list, [call(42, {"$set": {"controller_msg": False}}),
                                                 call(42, {"$unset": {"controller_msg": ""}})])

    async def test_skip_vote_stays_private_when_controller_messages_are_disabled(self):
        player = sample_player()
        player.settings = {"controller_msg": False}
        player.is_playing = True
        player.is_privileged = Mock(return_value=False)
        player.required = Mock(return_value=2)
        player.skip_votes = set()
        player.stop = AsyncMock()
        interaction = SimpleNamespace(user=object())
        button = Skip(player=player, btn_data={"emoji": "⏭️"}, icon_only=True)

        with patch("voicelink.views.controller.send_localized_message", new_callable=AsyncMock) as send:
            await button.callback(interaction)

        player.stop.assert_not_awaited()
        self.assertIn(interaction.user, player.skip_votes)
        self.assertEqual(send.await_args.args[1], "player.controls.skip.vote")
        self.assertTrue(send.await_args.kwargs["ephemeral"])

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
        self.assertEqual(rows[0][4]["style"], discord.ButtonStyle.secondary.value)
        self.assertEqual(rows[1][2]["style"], discord.ButtonStyle.secondary.value)
        self.assertEqual(rows[2][0]["style"], discord.ButtonStyle.success.value)
        self.assertFalse(rows[2][0]["disabled"])
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
        self.assertEqual(rows[1].children[2].style, discord.ButtonStyle.primary)
        self.assertIn("▰▰▰▰▰▱▱▱▱▱", view.progress.content)

    async def test_artist_url_from_loaded_playlist_links_the_entire_artist_line(self):
        player = sample_player()
        payload = {
            "encoded": "track",
            "info": {"title": "СЭИНТ ПИ", "author": "BOOKER, Крип-А-Крип", "uri": "https://music.yandex.ru/track/157583433",
                     "artworkUrl": player.current.thumbnail, "sourceName": "yandexmusic", "length": 180000},
            "pluginInfo": {"artistUrl": "https://music.yandex.ru/artist/4622993"},
        }
        node = SimpleNamespace(send=AsyncMock(return_value={"loadType": "playlist", "data": {"info": {}, "tracks": [payload]}}))
        playlist = await Node.get_tracks(node, "https://music.yandex.ru/track/157583433", requester=None,
                                         search_type=SearchType.YANDEX_MUSIC)
        player.current = playlist.tracks[0]
        player.get_msg = lambda key: "**Сейчас играет:**\n```{0}```"
        view = CompactController(player, discord.Embed())
        header = list(view.frame.children[0].children)
        self.assertEqual(header[0].content, "-# Сейчас играет")
        self.assertEqual(header[2].content, "[BOOKER, Крип-А-Крип](<https://music.yandex.ru/artist/4622993>)")

    async def test_queue_end_and_player_buttons_open_the_same_modal(self):
        player = sample_player()
        player.channel = object()
        player.node = SimpleNamespace(_available=True)
        player.is_user_join = Mock(return_value=True)
        player._ph = SimpleNamespace(replace=lambda text, _: text)
        player.settings = {}
        player.inactive_cleanup_at = 1795408167
        playing = CompactController(player, discord.Embed())
        player.current = None
        player.queue.is_empty = True
        player.queue.history = lambda **_: [object()]
        ended = CompactQueueEnd(player)
        ended_payload = ended.to_components()[0]["components"]
        self.assertIn("Очередь закончилась", ended_payload[0]["content"])
        self.assertIn("<t:1795408167:R>", ended_payload[2]["content"])
        buttons = ended_payload[3]["components"]
        self.assertEqual([button["label"] for button in buttons], ["Добавить трек", "Предыдущий трек", "Выйти из канала"])
        self.assertFalse(buttons[1]["disabled"])

        for view, button in ((playing, playing.frame.children[-1].children[0]), (ended, ended.children[0].children[-1].children[0])):
            response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(), send_modal=AsyncMock())
            interaction = SimpleNamespace(user=Mock(id=1), data={"custom_id": button.custom_id}, response=response)
            self.assertTrue(await view.interaction_check(interaction))
            response.defer.assert_not_awaited()
            await button.callback(interaction)
            modal, = response.send_modal.await_args.args
            self.assertIsInstance(modal, AddTrackModal)
            self.assertEqual([item.text for item in modal.children], ["Запрос", "Количество", "Отступ"])

    async def test_add_modal_submits_quantity_and_offset_and_refreshes_current_card(self):
        player = sample_player()
        player.accepts_tracks = True
        player.is_user_join = Mock(return_value=True)
        player.node = SimpleNamespace(_available=True)
        player.is_playing = True
        player.invoke_controller = AsyncMock()
        tracks = [object() for _ in range(6)]
        player.get_tracks = AsyncMock(return_value=tracks)
        player.add_track = AsyncMock()
        interaction = SimpleNamespace(guild=SimpleNamespace(voice_client=player), user=Mock(id=2),
                                      response=SimpleNamespace(defer=AsyncMock()))
        modal = AddTrackModal(player)
        modal.query._value = "playlist"
        modal.quantity._value = "3"
        modal.offset._value = "2"
        with patch("cogs.basic.send_localized_message", new_callable=AsyncMock):
            await modal.on_submit(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        player.get_tracks.assert_awaited_once_with("playlist", requester=interaction.user)
        player.add_track.assert_awaited_once_with(tracks[2:5], start_time=0, end_time=0)
        player.invoke_controller.assert_awaited_once_with()

    async def test_repeat_color_follows_current_mode_and_compact_volume_changes_by_ten(self):
        player = sample_player()
        player.is_privileged = Mock(return_value=True)
        player.set_repeat = AsyncMock(side_effect=lambda **_: player.queue._repeat.next())
        player.set_volume = AsyncMock(side_effect=lambda volume, _: setattr(player, "volume", volume))
        view = CompactController(player, discord.Embed())
        row = next(item for item in view.frame.children if isinstance(item, discord.ui.ActionRow))
        interaction = SimpleNamespace(user=object(), edit_original_response=AsyncMock())
        repeat = row.children[4]
        for mode, style in ((LoopType.TRACK, discord.ButtonStyle.primary), (LoopType.QUEUE, discord.ButtonStyle.success),
                            (LoopType.OFF, discord.ButtonStyle.secondary)):
            await repeat.callback(interaction)
            self.assertEqual(player.queue._repeat.mode, mode)
            self.assertEqual(repeat.style, style)

        await row.children[1].callback(interaction)
        self.assertEqual(view.volume_readout.label, "90%")
        await row.children[3].callback(interaction)
        self.assertEqual(player.set_volume.await_args_list, [call(90, interaction.user), call(100, interaction.user)])

    async def test_pause_button_turns_blue_only_when_play_is_available(self):
        player = sample_player()
        player.is_privileged = Mock(return_value=True)
        player.pause_votes = set()
        player.resume_votes = set()
        player.set_pause = AsyncMock(side_effect=lambda pause, _: setattr(player, "is_paused", pause))
        view = CompactController(player, discord.Embed())
        buttons = [item for item in view.frame.children if isinstance(item, discord.ui.ActionRow)]
        toggle = buttons[1].children[2]
        interaction = SimpleNamespace(user=object(), edit_original_response=AsyncMock())

        await toggle.callback(interaction)
        self.assertEqual(toggle.emoji.id, ICONS["play"][1])
        self.assertEqual(toggle.style, discord.ButtonStyle.primary)
        await toggle.callback(interaction)
        self.assertEqual(toggle.emoji.id, ICONS["pause"][1])
        self.assertEqual(toggle.style, discord.ButtonStyle.secondary)

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
            self.assertEqual(metadata, f"-# <:ym_yamusic:1234567> Яндекс Музыка  •  {size} в очереди  •  Добавлен: <@123456>")
        player.current.source = "vkmusic"
        self.assertEqual(view.metadata_text(), "-# <:ym_vkmusic:1374258169722703934> ВК Музыка  •  10 в очереди  •  Добавлен: <@123456>")

    async def test_progress_refresh_is_throttled_and_does_not_edit_a_replaced_track(self):
        player = sample_player()
        view = CompactController(player, discord.Embed())
        player._controller_view = view
        player._last_position = 120000
        player._closing = player._updating = False
        player._progress_edit_task = None
        player.controller = SimpleNamespace(edit=AsyncMock())
        player._edit_controller_progress = Player._edit_controller_progress.__get__(player)

        Player._schedule_controller_progress(player)
        await player._progress_edit_task
        self.assertIn("02:00", view.progress.content)
        self.assertEqual(player.controller.edit.await_args.kwargs["view"], view)

        player._last_position = 125000
        Player._schedule_controller_progress(player)
        self.assertIn("02:00", view.progress.content)
        player.current = sample_player().current
        view.next_progress_at = 0
        Player._schedule_controller_progress(player)
        player.controller.edit.assert_awaited_once()

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
                                 _schedule_inactive_cleanup_timer=Mock(),
                                 update_voice_status=AsyncMock(), do_next=AsyncMock(), playback_stopped=False)
        await Player.stop_playback(player)
        player.stop.assert_awaited_once_with(unpause=True)
        player.queue.clear.assert_called_once_with()
        self.assertTrue(player.playback_stopped)
        await Listeners.on_voicelink_track_end(None, player, object(), "stopped")
        player.do_next.assert_not_awaited()
