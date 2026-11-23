import asyncio
import unittest
import _bootstrap

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from voicelink import Config, LangHandler, MongoDBHandler, Player, get_playback_player
from voicelink.enums import RequestMethod
from voicelink.exceptions import NodeException, VoicelinkException
from voicelink.ipc.methods import getLyrics
from voicelink.lyrics import split_lyrics
from voicelink.views.controller import Lyrics
from voicelink.views.lyrics import LyricsView
from voicelink.voice_status import VoiceStatusUpdater


def connected_player(logger, channel):
    guild = SimpleNamespace(id=42, voice_client=None, change_voice_state=AsyncMock())
    channel.guild = guild
    channel._get_voice_client_key = Mock(return_value=(guild.id, None))
    clear_client = Mock(side_effect=lambda _: setattr(guild, "voice_client", None))
    client = SimpleNamespace(ipc_client=None, user=SimpleNamespace(id=1),
                             _connection=SimpleNamespace(_remove_voice_client=clear_client))
    node = SimpleNamespace(_logger=logger, _session_id="session", players={}, send=AsyncMock())
    with patch("voicelink.player.NodePool.get_node", return_value=node):
        player = Player(client, channel, SimpleNamespace(author=None), {})
    guild.voice_client = player
    node.players[guild.id] = player
    player._is_connected = True
    return player


class VoiceStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.wake = asyncio.Event()
        self.delays = []
        self.channel = SimpleNamespace(id=42, edit=AsyncMock())
        self.logger = Mock()
        self.updater = VoiceStatusUpdater(self.logger)
        self.clock_patch = patch('voicelink.voice_status.monotonic', side_effect=lambda: self.now)
        self.sleep_patch = patch('voicelink.voice_status.sleep', new=self.sleep)
        self.clock_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    async def asyncTearDown(self):
        await self.updater.close()

    async def sleep(self, seconds):
        self.delays.append(seconds)
        await self.wake.wait()
        self.wake.clear()
        self.now += seconds

    async def tick(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_repeated_identical_status_sends_one_request(self):
        for _ in range(100):
            self.updater.update(self.channel, 'track')
            await self.tick()
        self.channel.edit.assert_awaited_once_with(status='track')

    async def test_burst_keeps_only_latest_status(self):
        for number in range(100):
            self.updater.update(self.channel, str(number))
        await self.tick()
        self.channel.edit.assert_awaited_once_with(status='99')

    async def test_track_changes_wait_and_coalesce_without_blocking(self):
        self.updater.update(self.channel, 'first')
        await self.tick()
        for number in range(100):
            self.updater.update(self.channel, str(number))
            await self.tick()
        self.assertEqual(self.channel.edit.await_count, 1)
        self.assertEqual(self.delays, [60.0])
        self.wake.set()
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 2)
        self.channel.edit.assert_awaited_with(status='99')

    async def test_return_to_current_status_does_not_send_another_request(self):
        self.updater.update(self.channel, 'first')
        await self.tick()
        self.updater.update(self.channel, 'second')
        await self.tick()
        self.updater.update(self.channel, 'first')
        self.wake.set()
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 1)

    async def test_updates_during_request_keep_one_worker(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def edit(**kwargs):
            entered.set()
            await release.wait()
        self.channel.edit.side_effect = edit
        self.updater.update(self.channel, 'first')
        await entered.wait()
        task = self.updater._task
        for number in range(100):
            self.updater.update(self.channel, str(number))
            self.assertIs(self.updater._task, task)
        release.set()
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 1)
        self.wake.set()
        await self.tick()
        self.channel.edit.assert_awaited_with(status='99')

    async def test_rate_limit_backs_off_and_retries_latest_status(self):
        error = discord.HTTPException(SimpleNamespace(status=429, reason='Too Many Requests'), {'message': 'rate limited'})
        failed = False
        async def edit(**kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise error
        self.channel.edit.side_effect = edit
        self.updater.update(self.channel, 'first')
        await self.tick()
        self.assertEqual(self.delays, [300.0])
        self.updater.update(self.channel, 'latest')
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 1)
        self.wake.set()
        await self.tick()
        self.channel.edit.assert_awaited_with(status='latest')
        self.logger.exception.assert_not_called()

    async def test_forbidden_requests_are_not_repeated_on_every_track(self):
        self.channel.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), {'code': 50013})
        self.updater.update(self.channel, 'first')
        await self.tick()
        for number in range(100):
            self.updater.update(self.channel, str(number))
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 1)
        self.assertEqual(self.delays, [300.0])
        self.logger.exception.assert_not_called()

    async def test_rate_limit_respects_longer_retry_after(self):
        self.channel.edit.side_effect = discord.HTTPException(SimpleNamespace(status=429, reason='Too Many Requests', headers={'Retry-After': '600'}), {'message': 'rate limited'})
        self.updater.update(self.channel, 'track')
        await self.tick()
        self.assertEqual(self.delays, [600.0])

    async def test_request_failure_does_not_drop_newer_pending_status(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def edit(*, status):
            if status == 'first':
                entered.set()
                await release.wait()
                raise OSError('unavailable')
        self.channel.edit.side_effect = edit
        self.updater.update(self.channel, 'first')
        await entered.wait()
        self.updater.update(self.channel, 'latest')
        release.set()
        await self.tick()
        self.wake.set()
        await self.tick()
        self.channel.edit.assert_awaited_with(status='latest')

    async def test_close_cancels_delayed_update_and_clears_once(self):
        self.updater.update(self.channel, 'first')
        await self.tick()
        self.updater.update(self.channel, 'pending')
        await self.tick()
        task = self.updater._task
        await self.updater.close()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.updater._task)
        self.channel.edit.assert_awaited_with(status=None)
        await self.updater.close()
        self.updater.update(self.channel, 'ignored')
        await self.tick()
        self.assertEqual(self.channel.edit.await_count, 2)

    async def test_close_cancels_inflight_request(self):
        entered = asyncio.Event()
        async def edit(*, status):
            if status is not None:
                entered.set()
                await asyncio.Event().wait()
        self.channel.edit.side_effect = edit
        self.updater.update(self.channel, 'first')
        await entered.wait()
        task = self.updater._task
        await self.updater.close()
        self.assertTrue(task.cancelled())
        self.channel.edit.assert_awaited_with(status=None)

    async def test_close_network_error_does_not_block_disconnect(self):
        self.updater.update(self.channel, 'first')
        await self.tick()
        self.channel.edit.side_effect = OSError('unavailable')
        await self.updater.close()
        self.assertIsNone(self.updater._channel)
        self.logger.exception.assert_called_once()

    async def test_slow_request_times_out_and_releases_worker(self):
        self.updater.request_timeout = 0.01
        async def edit(**kwargs):
            await asyncio.Event().wait()
        self.channel.edit.side_effect = edit
        self.updater.update(self.channel, 'first')
        await self.updater._task
        self.assertIsNone(self.updater._task)
        self.logger.exception.assert_called_once()
        self.channel.edit.side_effect = None

    async def test_player_removal_cancels_updater_even_if_template_disabled(self):
        player = SimpleNamespace(_voice_status_updater=self.updater, settings={'stage_announce_template': ''}, channel=self.channel)
        self.updater.update(self.channel, 'track')
        await self.tick()
        await Player.update_voice_status(player, remove_status=True)
        self.channel.edit.assert_awaited_with(status=None)
        self.assertTrue(self.updater._closed)

    async def test_player_disconnect_cancels_pending_status_worker(self):
        self.updater.update(self.channel, 'first')
        await self.tick()
        self.updater.update(self.channel, 'pending')
        await self.tick()
        task = self.updater._task
        player = connected_player(self.logger, self.channel)
        player._voice_status_updater = self.updater
        await player.disconnect(force=True)
        self.assertTrue(task.cancelled())
        self.assertIsNone(player.channel)
        self.assertFalse(player._is_connected)
        self.assertIsNone(player.guild.voice_client)
        self.assertEqual(player.node.players, {})
        player.node.send.assert_awaited_once_with(RequestMethod.DELETE, "sessions/session/players/42")
        self.channel.edit.assert_awaited_with(status=None)

    async def test_player_queues_status_without_waiting_for_discord(self):
        self.channel.type = discord.ChannelType.voice
        self.channel.edit.side_effect = lambda **kwargs: None
        player = SimpleNamespace(_voice_status_updater=self.updater, _closing=False, channel=self.channel, settings={'stage_announce_template': 'track'}, _ph=SimpleNamespace(variables={}, replace=Mock(return_value='track')), _logger=self.logger)
        for _ in range(100):
            await Player.update_voice_status(player)
        self.channel.edit.assert_not_awaited()
        await self.tick()
        self.channel.edit.assert_awaited_once_with(status='track')


class PlayerDisconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.player = connected_player(Mock(), SimpleNamespace(id=42))
        self.guild = self.player.guild
        self.node = self.player.node

    async def test_disconnect_waits_for_pending_update_and_survives_caller_cancellation(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(method, query, data=None):
            if method == RequestMethod.PATCH:
                started.set()
                await release.wait()

        self.node.send.side_effect = send
        update = asyncio.create_task(self.player.send(RequestMethod.PATCH))
        await started.wait()
        first_close = asyncio.create_task(self.player.disconnect())
        await asyncio.sleep(0)
        first_close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_close
        late_update = asyncio.create_task(self.player.send(RequestMethod.PATCH))
        self.assertIs(self.guild.voice_client, self.player)
        self.node.send.assert_awaited_once()
        release.set()
        await asyncio.gather(update, late_update, self.player.disconnect(), self.player.destroy())

        self.assertEqual([call.args[0] for call in self.node.send.await_args_list],
                         [RequestMethod.PATCH, RequestMethod.DELETE])
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(self.node.players, {})

    async def test_old_disconnect_preserves_new_voice_client_on_same_node(self):
        replacement = SimpleNamespace(node=self.node)
        self.guild.voice_client = replacement
        await self.player.disconnect()

        self.assertIs(self.guild.voice_client, replacement)
        self.assertEqual(self.node.players, {})
        self.guild.change_voice_state.assert_not_awaited()
        self.node.send.assert_not_awaited()

    async def test_old_disconnect_preserves_replacement_in_node_pool(self):
        replacement = SimpleNamespace(node=self.node)
        self.guild.voice_client = replacement
        self.node.players[self.guild.id] = replacement
        await self.player.destroy()

        self.assertIs(self.guild.voice_client, replacement)
        self.assertIs(self.node.players[self.guild.id], replacement)
        self.node.send.assert_not_awaited()

    async def test_disconnect_retries_failed_node_delete(self):
        self.node.send.side_effect = [NodeException(), None]
        await self.player.disconnect()

        self.assertEqual(self.node.send.await_count, 2)
        self.assertEqual(self.node.players, {})
        self.assertIsNone(self.guild.voice_client)

    async def test_disconnect_clears_cached_client_when_channel_is_missing(self):
        self.player.channel = None
        await self.player.disconnect()

        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(self.node.players, {})
        self.node.send.assert_awaited_once_with(RequestMethod.DELETE, "sessions/session/players/42")

    async def test_external_kick_releases_both_players_before_saving_settings(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def save(*args):
            entered.set()
            await release.wait()

        with patch.object(MongoDBHandler, "update_settings", side_effect=save):
            kicked = asyncio.create_task(self.player.on_voice_state_update({"channel_id": None}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertIsNone(self.guild.voice_client)
                self.assertEqual(self.node.players, {})
                self.node.send.assert_awaited_once_with(RequestMethod.DELETE, "sessions/session/players/42")
                self.guild.change_voice_state.assert_not_awaited()
                self.assertFalse(kicked.done())
            finally:
                release.set()
                await kicked

    async def test_play_reconnects_when_discord_has_lost_the_cached_voice_session(self):
        self.guild.me = SimpleNamespace(voice=None)
        self.player._voice_state["sessionId"] = "old-session"
        ctx = SimpleNamespace(guild=self.guild)
        replacement = object()

        with patch.object(MongoDBHandler, "update_settings", new=AsyncMock()), \
             patch("voicelink.player.connect_channel", return_value=replacement) as connect:
            self.assertIs(await get_playback_player(ctx), replacement)

        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(self.node.players, {})
        self.guild.change_voice_state.assert_not_awaited()
        connect.assert_awaited_once_with(ctx)

    async def test_leave_waits_for_discord_before_releasing_voice_cache(self):
        self.guild.me = SimpleNamespace(voice=SimpleNamespace(channel=self.player.channel))
        self.player._voice_state["sessionId"] = "active-session"
        sent = asyncio.Event()
        self.guild.change_voice_state.side_effect = lambda **kwargs: sent.set()
        closing = asyncio.create_task(self.player.disconnect())
        try:
            await asyncio.wait_for(sent.wait(), timeout=1)
            self.assertIs(self.guild.voice_client, self.player)
            self.assertEqual(self.node.players, {})
        finally:
            await self.player.on_voice_state_update({"channel_id": None})
            await closing
        self.assertIsNone(self.guild.voice_client)

    async def test_inflight_search_cannot_enqueue_after_disconnect_starts(self):
        self.player._closing = True
        self.player.get_msg = Mock(return_value="no player")
        with self.assertRaises(VoicelinkException):
            await self.player.add_track(object())
        self.assertTrue(self.player.queue.is_empty)

    async def test_queued_playback_waits_for_lavalink_voice_registration(self):
        track = SimpleNamespace(position=0, requester=SimpleNamespace(bot=True))
        self.player.queue = SimpleNamespace(is_empty=False, get=Mock(return_value=track))
        self.player._ipc_client = SimpleNamespace(_is_connected=False)
        self.player.play = AsyncMock()
        self.player.invoke_controller = AsyncMock()
        self.player.update_voice_status = AsyncMock()
        pending = asyncio.create_task(self.player.do_next())
        await asyncio.sleep(0)
        self.player.queue.get.assert_not_called()
        self.player._voice_ready.set()
        await pending
        self.player.play.assert_awaited_once_with(track, start=0)


class LyricsPaginationTests(unittest.TestCase):
    def test_single_line_without_final_newline_is_preserved(self):
        self.assertEqual(split_lyrics('single line'), ['single line'])

    def test_last_line_is_preserved_across_page_boundaries(self):
        text = '\n'.join(str(index) for index in range(50))
        pages = split_lyrics(text)
        self.assertEqual(''.join(pages), text)
        self.assertEqual(len(pages), 3)
        self.assertTrue(all(len(page.splitlines()) <= 23 for page in pages))

    def test_long_lines_fit_embed_without_losing_characters(self):
        text = 'я' * 10000 + '\nlast line'
        pages = split_lyrics(text)
        self.assertEqual(''.join(pages), text)
        self.assertTrue(all(len(page) <= 4096 for page in pages))

    def test_empty_or_missing_lyrics_have_no_pages(self):
        for text in [None, '', ' \n\t\r\n']:
            self.assertEqual(split_lyrics(text), [])

    def test_windows_line_endings_are_preserved(self):
        text = '\r\n'.join(str(index) for index in range(30))
        self.assertEqual(''.join(split_lyrics(text)), text)


class LyricsButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.author = SimpleNamespace(id=42, guild=SimpleNamespace(id=42), display_avatar=SimpleNamespace(url='https://example.com/avatar.png'))
        self.player = SimpleNamespace(current=SimpleNamespace(title='song', author='artist'), is_playing=True, settings={})
        self.button = Lyrics(player=self.player, btn_data={})
        self.button.send = AsyncMock()
        self.button.send_embed = AsyncMock()
        self.interaction = SimpleNamespace(user=self.author, response=SimpleNamespace(is_done=Mock(return_value=True)))
        self.provider = SimpleNamespace(get_lyrics=AsyncMock())
        self.platform_patch = patch('voicelink.LYRICS_PLATFORMS', {Config().lyrics_platform: lambda: self.provider})
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.settings_patch = patch.object(MongoDBHandler, 'get_settings', new=AsyncMock(return_value={'lang': 'EN'}))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    async def test_empty_provider_response_uses_existing_not_found_message(self):
        for lyrics in [None, {}, {'default': None}, {'default': ''}, {'default': ' \n '}]:
            self.provider.get_lyrics.return_value = lyrics
            await self.button.callback(self.interaction)
            self.button.send.assert_awaited_with(self.interaction, 'lyrics.notFound', ephemeral=True)
        self.button.send_embed.assert_not_awaited()

    async def test_single_line_button_displays_lyrics(self):
        self.provider.get_lyrics.return_value = {'default': 'single line'}
        await self.button.callback(self.interaction)
        call = self.button.send_embed.await_args
        self.assertEqual(call.args[1].description, 'single line')
        call.kwargs['view'].stop()
        self.button.send.assert_not_awaited()

    async def test_non_default_lyrics_section_is_displayed(self):
        self.provider.get_lyrics.return_value = {'default': '', 'English': 'lyrics'}
        await self.button.callback(self.interaction)
        call = self.button.send_embed.await_args
        self.assertEqual(call.args[1].description, 'lyrics')
        call.kwargs['view'].stop()

    async def test_empty_view_builds_existing_not_found_message(self):
        view = LyricsView('song', {}, self.author)
        embed = await view.build_embed()
        self.assertEqual(embed.description, LangHandler._get_lang('EN', 'lyrics.notFound'))
        view.stop()

    async def test_ipc_preserves_last_line(self):
        self.provider.get_lyrics.return_value = {'default': 'first\nlast'}
        with patch('voicelink.ipc.methods.LYRICS_PLATFORMS', {Config().lyrics_platform: lambda: self.provider}):
            result = await getLyrics(Mock(), {'title': 'song'})
        self.assertEqual(result['lyrics'], {'default': ['first\nlast']})


if __name__ == '__main__':
    unittest.main()
