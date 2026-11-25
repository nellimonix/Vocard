import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import _bootstrap

from voicelink import LangHandler, Node, NodePool, TrackLoadError
from voicelink.enums import RequestMethod
from voicelink.exceptions import NodeHTTPError
from voicelink.utils import track_load_error_text


def node(identifier, response, *, source="yandexmusic"):
    value = SimpleNamespace(
        _identifier=identifier, _available=True, is_connected=True, players={},
        supports=lambda name: name == source, send=AsyncMock(return_value=response),
    )
    value.get_tracks = partial(Node.get_tracks, value)
    return value


class LookupFailoverTests(unittest.IsolatedAsyncioTestCase):
    fault = {"loadType": "error", "data": {
        "message": "Something went wrong while looking up the track.",
        "severity": "fault",
        "causeStackTrace": (
            "java.lang.IllegalStateException: Response code from channel info is 451\n"
            "\tat example.Source.getJson(Source.java:499)\n"
            "https://example.com/private?token=secret"
        ),
    }}

    async def test_retries_once_on_another_compatible_connected_node(self):
        primary = node("MAIN", self.fault)
        alternate = node("SECONDARY", {"loadType": "search", "data": [{}]})
        disabled = node("A", {"loadType": "empty"})
        disabled._available = False
        incompatible = node("B", {"loadType": "empty"}, source="vkmusic")
        with (
            patch.object(NodePool, "_nodes", {n._identifier: n for n in (primary, disabled, incompatible, alternate)}),
            patch("voicelink.pool.Track.from_lavalink", return_value="track"),
            self.assertLogs("vocard.lavalink", level="INFO") as logs,
        ):
            self.assertEqual(await NodePool.load_tracks("private query", requester=None, node=primary), ["track"])
        primary.send.assert_awaited_once()
        alternate.send.assert_awaited_once()
        disabled.send.assert_not_awaited()
        incompatible.send.assert_not_awaited()
        self.assertIn("status=451", logs.output[0])
        self.assertIn("Source.java:499", logs.output[0])
        self.assertIn("outcome='recovered'", logs.output[1])
        self.assertEqual(logs.output[0].split("trace_id=")[1].split()[0], logs.output[1].split("trace_id=")[1].split()[0])
        self.assertNotIn("private query", str(logs.output))
        self.assertNotIn("token=secret", str(logs.output))

    async def test_common_error_and_empty_result_do_not_use_another_node(self):
        alternate = node("SECONDARY", {"loadType": "empty"})
        common = {"loadType": "error", "data": {"severity": "common", "message": "private details"}}
        primary = node("MAIN", common)
        with patch.object(NodePool, "_nodes", {"MAIN": primary, "SECONDARY": alternate}):
            with self.assertLogs("vocard.lavalink"), self.assertRaises(TrackLoadError) as raised:
                await NodePool.load_tracks("query", requester=None, node=primary)
            self.assertNotIn("private details", str(raised.exception))
            primary.send.return_value = {"loadType": "empty"}
            self.assertIsNone(await NodePool.load_tracks("query", requester=None, node=primary))
        alternate.send.assert_not_awaited()

    async def test_stops_after_the_alternate_also_fails(self):
        primary = node("MAIN", self.fault)
        alternate = node("SECONDARY", self.fault)
        third = node("THIRD", {"loadType": "empty"})
        with (
            patch.object(NodePool, "_nodes", {n._identifier: n for n in (primary, alternate, third)}),
            self.assertLogs("vocard.lavalink") as logs,
            self.assertRaises(TrackLoadError) as raised,
        ):
            await NodePool.load_tracks("query", requester=None, node=primary)
        error = raised.exception
        self.assertEqual(error.failure, "source_restricted")
        self.assertRegex(error.event_id, r"^\d{12}$")
        self.assertTrue(str(error).endswith(f"(#{error.event_id})"))
        self.assertNotIn("HTTP 451", str(error))
        for line in logs.output:
            self.assertIn(f"trace_id={error.event_id} ", line)
        self.assertIn("outcome='failed'", logs.output[-1])
        third.send.assert_not_awaited()

    async def test_localized_reply_contains_the_number_from_the_error(self):
        error = TrackLoadError(event_id="1241")
        replies = {
            "RU": "Не удалось загрузить трек. Попробуйте ещё раз позже. (#1241)",
            "EN": "Couldn’t load the track. Please try again later. (#1241)",
        }
        for language, expected in replies.items():
            with self.subTest(language=language):
                template = LangHandler._get_lang(language, "player.errors.trackLoadFailed")
                with patch.object(LangHandler, "get_lang", new=AsyncMock(return_value=template)):
                    self.assertEqual(await track_load_error_text(42, error), expected)

    async def test_http_errors_record_method_status_and_stack_without_payload(self):
        async def send(*args):
            raise NodeHTTPError(502)

        primary = SimpleNamespace(_identifier="MAIN", _send=send)
        with self.assertLogs("vocard.lavalink") as logs, self.assertRaises(NodeHTTPError):
            await Node.send(primary, RequestMethod.PATCH, "sessions/private/players/42", {"voice": {"token": "secret"}})
        output = logs.output[0]
        for value in ("status=502", "method='PATCH'", "operation='sessions'", "action='voice'", "NodeHTTPError"):
            self.assertIn(value, output)
        for value in ("private", "secret"):
            self.assertNotIn(value, output)
