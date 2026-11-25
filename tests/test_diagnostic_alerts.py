import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import _bootstrap

from voicelink.diagnostic_alerts import DiscordDiagnostics
from voicelink.diagnostics import DiagnosticEvent, lookup_report, record, set_report_sink


def lookup_events(outcome, event_id="1241"):
    events = [DiagnosticEvent("lavalink_lookup_failed", event_id, {
        "node": "MAIN", "source": "yandexmusic", "category": "source_restricted",
        "status": 451, "severity": "fault", "exception_types": ("java.lang.IllegalStateException",),
        "frames": ("example.Source.lookup(Source.java:499)",),
    })]
    if outcome == "failed":
        events.append(DiagnosticEvent("lavalink_lookup_failed", event_id, {
            "node": "SECONDARY", "source": "yandexmusic", "category": "node_timeout",
            "traceback": (("TimeoutError", ("pool.py:534 get_tracks",)),),
        }))
    events.append(DiagnosticEvent("lavalink_lookup_failover", event_id, {
        "node": "MAIN", "alternative": "SECONDARY", "outcome": outcome,
    }))
    return tuple(events)


class DiagnosticAlertsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.guild = SimpleNamespace(id=42)
        self.message = SimpleNamespace(edit=AsyncMock())
        self.channel.send = AsyncMock(return_value=self.message)
        self.bot = SimpleNamespace(get_channel=Mock(return_value=None), fetch_channel=AsyncMock(return_value=self.channel))
        self.alerts = DiscordDiagnostics(self.bot, guild_id=42, channel_id=43)

    async def test_recovered_lookup_creates_one_embed_and_counts_repeats(self):
        now = time.monotonic()
        self.alerts._ingest(lookup_events("recovered"), now)
        await self.alerts._flush(now)
        args = self.channel.send.await_args.kwargs
        self.assertEqual(args["nonce"], "1241")
        self.assertEqual(args["allowed_mentions"].to_dict(), {"parse": []})
        self.assertEqual(args["embed"].title, "YaMusicBot · Ошибка поиска трека")
        self.assertIn("HTTP 451", args["embed"].description)
        self.assertIn("Трек найден", [field.value for field in args["embed"].fields])

        self.alerts._ingest(lookup_events("recovered", event_id="1242"), now + 1)
        await self.alerts._flush(now + 300)
        self.channel.send.assert_awaited_once()
        self.message.edit.assert_awaited_once()
        edited = self.message.edit.await_args.kwargs["embed"]
        self.assertEqual(edited.fields[-1].value, "1")
        self.assertNotIn("attachments", self.message.edit.await_args.kwargs)

    async def test_failed_lookup_includes_both_attempts_under_its_number(self):
        now = time.monotonic()
        self.alerts._ingest(lookup_events("failed"), now)
        await self.alerts._flush(now)
        args = self.channel.send.await_args.kwargs
        embed = args["embed"]
        self.assertEqual(embed.title, "YaMusicBot · Ошибка загрузки трека")
        self.assertIn("HTTP 451", embed.description)
        self.assertIn("SECONDARY", embed.description)
        self.assertEqual(embed.fields[0].value, "#1241")
        attachment = args["file"]
        self.assertEqual(attachment.filename, "traceback.txt")
        self.assertIn("Событие #1241", attachment.fp.getvalue().decode())
        self.assertIn("TimeoutError", attachment.fp.getvalue().decode())

    async def test_python_exception_shows_type_and_frame(self):
        event = DiagnosticEvent("discord_command_failed", "1242", {
            "command": "play", "traceback": (("KeyError", ("basic.py:54 enqueue_search",)),),
        })
        now = time.monotonic()
        self.alerts._ingest((event,), now)
        await self.alerts._flush(now)
        args = self.channel.send.await_args.kwargs
        self.assertEqual(args["embed"].title, "YaMusicBot · Исключение в коде бота")
        self.assertIn("KeyError", args["file"].fp.getvalue().decode())
        self.assertIn("basic.py:54", args["file"].fp.getvalue().decode())

    async def test_rest_and_playback_inputs_have_distinct_titles(self):
        events = (
            DiagnosticEvent("lavalink_request_failed", "1243", {
                "node": "MAIN", "method": "PATCH", "operation": "sessions", "action": "voice", "status": 502,
            }),
            DiagnosticEvent("lavalink_playback_failed", "1244", {
                "node": "MAIN", "category": "source_timeout",
                "exception_types": ("java.net.SocketTimeoutException",),
            }),
        )
        now = time.monotonic()
        for event in events:
            self.alerts._ingest((event,), now)
        await self.alerts._flush(now)
        rest, playback = (call.kwargs for call in self.channel.send.await_args_list)
        self.assertEqual(rest["embed"].title, "YaMusicBot · Ошибка запроса к Lavalink")
        self.assertIn("HTTP 502", [field.value for field in rest["embed"].fields])
        self.assertEqual(playback["embed"].title, "YaMusicBot · Ошибка воспроизведения")
        self.assertIn("SocketTimeoutException", playback["file"].fp.getvalue().decode())

    async def test_channel_from_another_guild_is_rejected(self):
        self.channel.guild.id = 99
        now = time.monotonic()
        self.alerts._ingest(lookup_events("failed"), now)
        with self.assertLogs("vocard.diagnostics") as logs:
            await self.alerts._flush(now)
        self.assertIn("discord_diagnostics_delivery_failed", logs.output[0])
        self.channel.send.assert_not_awaited()

    async def test_confirmed_expiry_mentions_only_the_configured_user_once(self):
        self.alerts = DiscordDiagnostics(self.bot, guild_id=42, channel_id=43,
                                         yandex_music_tokens={"MAIN": "fake"}, plus_mention_user_id=44)
        check = AsyncMock(return_value={
            "account": {"uid": 123}, "plus": {"hasPlus": False}, "subscription": {"hadAnySubscription": True},
        })
        now = time.monotonic()
        with patch.object(self.alerts._plus, "_account_status", new=check):
            self.alerts._ingest(lookup_events("recovered"), now)
            await self.alerts._flush(now)
            await self.alerts._flush(now + 300)
            self.alerts._ingest(lookup_events("failed", event_id="1242"), now + 301)
            await self.alerts._flush(now + 301)
        first, second = (call.kwargs for call in self.channel.send.await_args_list)
        self.assertEqual(first["content"], "<@44>")
        self.assertEqual(first["allowed_mentions"].to_dict(), {"parse": [], "users": [44]})
        self.assertNotIn("content", second)
        self.assertEqual(first["embed"].title, "YaMusicBot · Яндекс Плюс закончился")
        self.assertEqual(first["embed"].description,
                         "На аккаунте ноды MAIN нет активного Плюса. Яндекс Музыка вернула HTTP 451.")
        self.assertEqual(first["embed"].fields[0].value, "#1241")
        self.assertTrue(any("SECONDARY" in field.value for field in second["embed"].fields))

    async def test_451_without_confirmed_subscription_expiry_sends_no_ping(self):
        self.alerts = DiscordDiagnostics(self.bot, guild_id=42, channel_id=43,
                                         yandex_music_tokens={"MAIN": "fake"}, plus_mention_user_id=44)
        check = AsyncMock(return_value={"account": {"uid": 123}, "plus": {"hasPlus": True}})
        now = time.monotonic()
        with patch.object(self.alerts._plus, "_account_status", new=check):
            self.alerts._ingest(lookup_events("recovered"), now)
            await self.alerts._flush(now)
        args = self.channel.send.await_args.kwargs
        self.assertNotIn("content", args)
        self.assertEqual(args["embed"].title, "YaMusicBot · Ошибка поиска трека")

    def test_lookup_releases_one_group_after_failover_finishes(self):
        sink = Mock()
        set_report_sink(sink)
        self.addCleanup(set_report_sink, None)
        logger = logging.getLogger("test.diagnostic_alerts")
        with self.assertLogs(logger):
            with lookup_report():
                event_id = record(logger, "lavalink_lookup_failed", node="MAIN")
                record(logger, "lavalink_lookup_failover", level="info", outcome="recovered")
                sink.assert_not_called()
        sink.assert_called_once()
        attempts = sink.call_args.args[0]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(event.event_id == event_id for event in attempts))

    async def test_worker_sends_a_completed_lookup_using_the_number_from_the_log(self):
        sent = asyncio.Event()

        async def send(**_):
            sent.set()
            return self.message

        self.channel.send.side_effect = send
        self.alerts.start()
        self.addAsyncCleanup(self.alerts.close)
        logger = logging.getLogger("test.diagnostic_alerts")
        with self.assertLogs(logger):
            with lookup_report():
                event_id = record(logger, "lavalink_lookup_failed", node="MAIN", status=451)
                record(logger, "lavalink_lookup_failover", level="info", node="MAIN",
                       alternative="SECONDARY", outcome="recovered")
        await asyncio.wait_for(sent.wait(), timeout=1)
        self.channel.send.assert_awaited_once()
        embed = self.channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.fields[0].value, f"#{event_id}")
