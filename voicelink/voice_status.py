import asyncio

from asyncio import sleep
from time import monotonic

from discord import errors


class VoiceStatusUpdater:
    interval = 60.0
    retry_interval = 300.0
    request_timeout = 10.0

    def __init__(self, logger):
        self._logger = logger
        self._pending = None
        self._last_sent = None
        self._channel = None
        self._next_update = 0.0
        self._task = None
        self._closed = False

    def update(self, channel, status):
        if self._closed:
            return
        self._channel = channel
        self._pending = (channel, status)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while self._pending is not None:
                channel, status = self._pending
                if (channel.id, status) == self._last_sent:
                    self._pending = None
                    continue
                delay = self._next_update - monotonic()
                if delay > 0:
                    await sleep(delay)
                    continue
                self._pending = None
                self._next_update = monotonic() + self.interval
                try:
                    async with asyncio.timeout(self.request_timeout):
                        await channel.edit(status=status)
                    self._last_sent = (channel.id, status)
                except (errors.Forbidden, errors.NotFound):
                    self._next_update = monotonic() + self.retry_interval
                except errors.HTTPException as error:
                    if error.status != 429:
                        self._logger.exception("Failed to update voice status")
                    else:
                        try:
                            retry_after = float(error.response.headers.get('Retry-After', 0))
                        except (AttributeError, TypeError, ValueError):
                            retry_after = 0
                        self._next_update = monotonic() + max(self.retry_interval, retry_after)
                        if self._pending is None:
                            self._pending = (channel, status)
                except Exception:
                    self._logger.exception("Failed to update voice status")
        except Exception:
            self._logger.exception("Failed to update voice status")
        finally:
            self._task = None

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._pending = None
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
        if self._channel is None:
            return
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._channel.edit(status=None)
        except (errors.HTTPException, TimeoutError):
            pass
        except Exception:
            self._logger.exception("Failed to clear voice status")
        finally:
            self._channel = None
