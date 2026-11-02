import asyncio
import unittest
import _bootstrap

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from voicelink import Config, LangHandler, MongoDBHandler, Player
from voicelink.ipc.methods import getLyrics
from voicelink.lyrics import split_lyrics
from voicelink.views.controller import Lyrics
from voicelink.views.lyrics import LyricsView
from voicelink.voice_status import VoiceStatusUpdater


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
        player = SimpleNamespace(_voice_status_updater=self.updater, _stop_controller_view=Mock(), guild=SimpleNamespace(name='test', id=42, change_voice_state=AsyncMock()), cleanup=Mock(), _logger=self.logger, channel=self.channel, _is_connected=True)
        await Player.disconnect(player)
        self.assertTrue(task.cancelled())
        self.assertIsNone(player.channel)
        self.assertFalse(player._is_connected)
        player.cleanup.assert_called_once()
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
