import logging
import time
from dataclasses import dataclass

import aiohttp


_logger = logging.getLogger("vocard.diagnostics")
_CHECK_INTERVAL = 300


@dataclass
class _Subscription:
    expired: bool = False
    notified: bool = False
    check_after: float = 0.0


class YandexPlusSubscriptions:
    def __init__(self, tokens: dict[str, str]):
        self._tokens = tokens
        self._checked = {}

    async def expired(self, node: str) -> bool:
        if node not in self._tokens:
            return False
        state = self._checked.setdefault(node, _Subscription())
        await self._refresh(node, state)
        return state.expired

    async def refresh_notified(self):
        for node, state in self._checked.items():
            if state.notified:
                await self._refresh(node, state)

    def needs_mention(self, node: str) -> bool:
        return not self._checked[node].notified

    def mark_notified(self, node: str):
        self._checked[node].notified = True

    async def _refresh(self, node: str, state: _Subscription):
        now = time.monotonic()
        if now < state.check_after:
            return
        state.check_after = now + _CHECK_INTERVAL
        state.expired = False
        try:
            status = await self._account_status(self._tokens[node])
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            _logger.warning("yandex_plus_check_failed node=%r error=%s status=%s",
                            node, type(error).__name__, getattr(error, "status", None))
            return
        if not isinstance(status, dict):
            return
        account = status.get("account")
        plus = status.get("plus")
        subscription = status.get("subscription")
        if not isinstance(account, dict) or not account.get("uid") or not isinstance(plus, dict):
            return
        has_plus = plus.get("hasPlus")
        if has_plus is True:
            state.notified = False
        elif has_plus is False and isinstance(subscription, dict) and subscription.get("hadAnySubscription") is True:
            state.expired = True

    async def _account_status(self, token: str) -> dict:
        headers = {
            "Authorization": f"OAuth {token}",
            "User-Agent": "Yandex-Music-API",
            "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get("https://api.music.yandex.net/account/status",
                                   headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    response.raise_for_status()
                    return {}
                payload = await response.json()
        return payload.get("result", {}) if isinstance(payload, dict) else {}
