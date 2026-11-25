import asyncio
import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import discord

from .diagnostics import DiagnosticEvent, set_report_sink
from .yandex_plus import YandexPlusSubscriptions


_logger = logging.getLogger("vocard.diagnostics")
_WINDOW_SECONDS = 300
_MAX_PENDING = 128
_TITLES = {
    "lavalink_lookup_failed": "Ошибка поиска трека",
    "lavalink_request_failed": "Ошибка запроса к Lavalink",
    "lavalink_player_request_timeout": "Ошибка запроса к Lavalink",
    "lavalink_playback_failed": "Ошибка воспроизведения",
    "discord_command_failed": "Исключение в коде бота",
    "discord_application_command_failed": "Исключение в коде бота",
}
_SOURCES = {"yandexmusic": "Яндекс Музыка", "vkmusic": "VK Музыка"}
_FINGERPRINT_FIELDS = ("node", "source", "alternative", "outcome", "status", "severity",
                       "category", "method", "operation", "action", "command")


def _safe(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:250]


def _reason(fields):
    if status := fields.get("status"):
        label = f"HTTP {status}"
        if fields.get("category", "").startswith("source_"):
            label = f"API источника вернул {label}"
        if severity := fields.get("severity"):
            label += f" ({_safe(severity)})"
        return label
    if fields.get("category") == "node_timeout":
        return "Запрос к Lavalink завершился по тайм-ауту."
    if trace := fields.get("traceback"):
        return _safe(trace[-1][0])
    return _safe(fields.get("category") or fields.get("severity") or "unknown")


def _embed(events, timestamp, repeats, expired_plus_node=None):
    first = events[0]
    fields = first.fields
    failover = events[-1].fields if events[-1].name == "lavalink_lookup_failover" else {}
    outcome = failover.get("outcome")
    recovered = outcome == "recovered"
    title = "Ошибка загрузки трека" if outcome == "failed" else _TITLES[first.name]
    if expired_plus_node:
        title = "Яндекс Плюс закончился"
    embed = discord.Embed(title=f"YaMusicBot · {title}", color=0xE0A33C if recovered else 0xD45151,
                          timestamp=timestamp)
    embed.add_field(name="Код события", value=f"#{first.event_id}")

    if first.name == "lavalink_lookup_failed":
        lines = []
        for event in events:
            if event.name == "lavalink_lookup_failed":
                node = _safe(event.fields["node"]) if event.fields.get("node") else None
                reason = _reason(event.fields)
                lines.append(f"**{node}**: {reason}" if node else reason)
        embed.description = "\n".join(lines)
        if source := fields.get("source"):
            embed.add_field(name="Источник", value=_safe(_SOURCES.get(source, source)))
        if alternative := failover.get("alternative"):
            embed.add_field(name="Переход", value=f"{_safe(fields['node'])} → {_safe(alternative)}")
        if recovered:
            embed.add_field(name="Результат", value="Трек найден")
        elif outcome == "empty":
            embed.add_field(name="Результат", value="empty")
        elif outcome == "unavailable":
            embed.add_field(name="Резервная нода", value="unavailable")
        if frames := fields.get("frames"):
            embed.add_field(name="Трассировка", value=f"\x60{_safe(frames[0])}\x60", inline=False)
    else:
        if node := fields.get("node"):
            embed.add_field(name="Нода", value=_safe(node))
        operation = " · ".join(_safe(fields[key]) for key in ("method", "operation", "action", "command")
                               if fields.get(key))
        if operation:
            embed.add_field(name="Операция", value=operation)
        trace = fields.get("traceback")
        if trace and first.name.startswith("discord_"):
            kind, frames = trace[-1]
            embed.add_field(name="Тип исключения", value=f"\x60{_safe(kind)}\x60")
            if frames:
                embed.add_field(name="Место сбоя", value=f"\x60{_safe(frames[-1])}\x60", inline=False)
        else:
            embed.add_field(name="Причина", value=_reason(fields), inline=False)

    if expired_plus_node:
        if embed.description:
            embed.add_field(name="Причина", value=embed.description, inline=False)
        embed.description = (
            f"На аккаунте ноды {_safe(expired_plus_node)} нет активного Плюса. Яндекс Музыка вернула HTTP 451."
        )
    embed.add_field(name="Повторы за 5 минут", value=str(repeats))
    return embed


def _traceback(events):
    blocks = []
    for event in events:
        fields = event.fields
        lines = list(fields.get("exception_types", ()))
        lines.extend(f"  at {frame}" for frame in fields.get("frames", ()))
        for kind, frames in reversed(fields.get("traceback", ())):
            lines.append(kind)
            lines.extend(f"  {frame}" for frame in frames)
        if lines:
            header = f"{fields['node']}\n" if fields.get("node") else ""
            blocks.append(header + "\n".join(lines))
    if blocks:
        return f"Событие #{events[0].event_id}\n\n" + "\n\n".join(blocks) + "\n"
    return None


def _fingerprint(events):
    return tuple(
        (event.name, *(event.fields.get(key) for key in _FINGERPRINT_FIELDS),
         event.fields.get("exception_types"), event.fields.get("traceback"))
        for event in events
    )


@dataclass
class _AlertWindow:
    events: tuple[DiagnosticEvent, ...]
    timestamp: datetime
    deadline: float
    repeats: int = 0
    message: discord.Message | None = None
    expired_plus_node: str | None = None


class DiscordDiagnostics:
    def __init__(self, bot, guild_id: int, channel_id: int, *, yandex_music_tokens=None, plus_mention_user_id=0):
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self._plus = YandexPlusSubscriptions(yandex_music_tokens or {})
        self._plus_mention_user_id = plus_mention_user_id
        self._channel = None
        self._queue = asyncio.Queue(maxsize=_MAX_PENDING)
        self._windows = {}
        self._retry_after = 0.0
        self._dropped = 0
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        set_report_sink(self.submit)

    async def close(self):
        set_report_sink(None)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def submit(self, events):
        if events[0].name not in _TITLES:
            return
        try:
            self._queue.put_nowait(events)
        except asyncio.QueueFull:
            self._dropped += 1

    def _ingest(self, events, now):
        key = _fingerprint(events)
        if window := self._windows.get(key):
            window.repeats += 1
        elif len(self._windows) < _MAX_PENDING:
            self._windows[key] = _AlertWindow(events, datetime.now(timezone.utc), now + _WINDOW_SECONDS)
        else:
            self._dropped += 1

    async def _run(self):
        while True:
            try:
                async with asyncio.timeout(5):
                    events = await self._queue.get()
            except TimeoutError:
                pass
            else:
                self._ingest(events, time.monotonic())
            await self._flush(time.monotonic())
            await self._plus.refresh_notified()
            if self._dropped:
                _logger.warning("discord_diagnostics_queue_full dropped=%s", self._dropped)
                self._dropped = 0

    async def _destination(self):
        if self._channel is None:
            channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild.id != self.guild_id:
                raise ValueError("invalid diagnostic destination")
            self._channel = channel
        return self._channel

    async def _flush(self, now):
        for key, window in tuple(self._windows.items()):
            expired = now >= window.deadline
            if now >= window.deadline + _WINDOW_SECONDS:
                del self._windows[key]
                continue
            if window.message and not expired:
                continue
            if window.message and not window.repeats:
                del self._windows[key]
                continue
            if time.monotonic() < self._retry_after:
                continue
            try:
                async with asyncio.timeout(15):
                    await self._deliver(window)
            except (discord.HTTPException, aiohttp.ClientError, TimeoutError, ValueError) as error:
                self._channel = None
                self._retry_after = time.monotonic() + 60
                _logger.warning("discord_diagnostics_delivery_failed error=%s status=%s",
                                type(error).__name__, getattr(error, "status", None))
                continue
            if expired:
                del self._windows[key]

    async def _deliver(self, window):
        if window.message is None:
            window.expired_plus_node = await self._confirmed_plus_expiry(window.events)
        embed = _embed(window.events, window.timestamp, window.repeats, window.expired_plus_node)
        allowed = discord.AllowedMentions.none()
        if window.message:
            await window.message.edit(embed=embed, allowed_mentions=allowed)
            return
        channel = await self._destination()
        options = {}
        expired_node = window.expired_plus_node
        mention = expired_node and self._plus_mention_user_id and self._plus.needs_mention(expired_node)
        if mention:
            recipient = discord.Object(id=self._plus_mention_user_id)
            options["content"] = f"<@{recipient.id}>"
            allowed = discord.AllowedMentions(everyone=False, roles=False, users=[recipient], replied_user=False)
        if trace := _traceback(window.events):
            options["file"] = discord.File(io.BytesIO(trace.encode("utf-8")), filename="traceback.txt")
        window.message = await channel.send(embed=embed, allowed_mentions=allowed,
                                            nonce=window.events[0].event_id, **options)
        if mention:
            self._plus.mark_notified(expired_node)

    async def _confirmed_plus_expiry(self, events):
        for event in events:
            fields = event.fields
            if fields.get("status") != 451 or fields.get("source") != "yandexmusic":
                continue
            node = fields.get("node")
            if node and await self._plus.expired(node):
                _logger.info("yandex_plus_expired trace_id=%s node=%r", event.event_id, node)
                return node
        return None
