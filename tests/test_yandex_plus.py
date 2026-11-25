import unittest
from unittest.mock import AsyncMock, patch

import _bootstrap

from voicelink.yandex_plus import YandexPlusSubscriptions


def status(plus, *, previous=True):
    return {"account": {"uid": 123}, "plus": {"hasPlus": plus},
            "subscription": {"hadAnySubscription": previous}}


class YandexPlusTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_former_subscriber_with_confirmed_disabled_plus_is_expired(self):
        monitor = YandexPlusSubscriptions({"MAIN": "token", "SECONDARY": "token2"})
        with patch.object(monitor, "_account_status", new=AsyncMock(side_effect=[status(False), status(False, previous=False)])):
            self.assertTrue(await monitor.expired("MAIN"))
            self.assertFalse(await monitor.expired("SECONDARY"))
            self.assertFalse(await monitor.expired("unconfigured"))

    async def test_renewal_rearms_the_mention_and_unavailable_api_does_not(self):
        monitor = YandexPlusSubscriptions({"MAIN": "token"})
        check = AsyncMock(side_effect=[status(False), TimeoutError(), status(True), status(False)])
        with patch.object(monitor, "_account_status", new=check), patch("voicelink.yandex_plus.time") as clock:
            clock.monotonic.return_value = 1
            self.assertTrue(await monitor.expired("MAIN"))
            monitor.mark_notified("MAIN")
            self.assertFalse(monitor.needs_mention("MAIN"))
            self.assertTrue(await monitor.expired("MAIN"))
            check.assert_awaited_once()
            clock.monotonic.return_value = 302
            with self.assertLogs("vocard.diagnostics"):
                self.assertFalse(await monitor.expired("MAIN"))
            self.assertFalse(monitor.needs_mention("MAIN"))
            clock.monotonic.return_value = 603
            await monitor.refresh_notified()
            self.assertTrue(monitor.needs_mention("MAIN"))
            clock.monotonic.return_value = 904
            self.assertTrue(await monitor.expired("MAIN"))
