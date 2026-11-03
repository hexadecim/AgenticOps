"""Background loops: poll the mailbox at the configured interval, purge expired raw emails hourly.

Run the poller in one replica only (`POLLER_ENABLED=false` elsewhere); the unique dedupe key keeps a second
poller harmless, but wasteful.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ...application import ActiveMailbox, PollMailbox, PurgeExpiredEmails

log = structlog.get_logger(__name__)


class Scheduler:
    def __init__(
        self,
        poll: PollMailbox,
        purge: PurgeExpiredEmails,
        active: ActiveMailbox,
        *,
        idle_interval: float = 30.0,
        retention_interval: float = 3600.0,
    ) -> None:
        self._poll, self._purge, self._active = poll, purge, active
        self._idle, self._retention = idle_interval, retention_interval
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def wake(self) -> None:
        """Poll now instead of waiting for the interval (settings changed)."""
        self._wake.set()

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="mail-poller"),
            asyncio.create_task(self._retention_loop(), name="mail-retention"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll()
            except Exception:
                log.exception("mail_poll_crashed")
            config = self._active.config
            interval = config.poll_interval_seconds if config else self._idle
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), interval)
            self._wake.clear()

    async def _retention_loop(self) -> None:
        await asyncio.sleep(60)  # let the service settle first
        while True:
            try:
                await self._purge()
            except Exception:
                log.exception("mail_retention_failed")
            await asyncio.sleep(self._retention)
