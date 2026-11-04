"""Background loops: poll the mailbox at the configured interval, send approved messages, purge expired raw
emails hourly.

Run the poller in one replica only (`POLLER_ENABLED=false` elsewhere); the unique dedupe key keeps a second
poller harmless, but wasteful.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ...application import ActiveMailbox, PollMailbox, PurgeExpiredEmails, SendDueMessages

log = structlog.get_logger(__name__)


class Scheduler:
    def __init__(
        self,
        poll: PollMailbox,
        purge: PurgeExpiredEmails,
        active: ActiveMailbox,
        send: SendDueMessages,
        *,
        idle_interval: float = 30.0,
        retention_interval: float = 3600.0,
        send_interval: float = 15.0,
    ) -> None:
        self._poll, self._purge, self._active, self._send = poll, purge, active, send
        self._idle, self._retention, self._send_interval = idle_interval, retention_interval, send_interval
        self._wake = asyncio.Event()
        self._wake_sender = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def wake(self) -> None:
        """Poll now instead of waiting for the interval (settings changed)."""
        self._wake.set()

    def wake_sender(self) -> None:
        """A message or its approval arrived: try sending now."""
        self._wake_sender.set()

    async def start(self) -> None:
        recovered = await self._send.recover()
        if recovered:
            log.warning("sends_interrupted", count=recovered)
        self._tasks = [
            asyncio.create_task(self._send_loop(), name="mail-sender"),
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

    async def _send_loop(self) -> None:
        while True:
            try:
                while await self._send():  # keep going while there is due work
                    pass
            except Exception:
                log.exception("mail_send_crashed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_sender.wait(), self._send_interval)
            self._wake_sender.clear()

    async def _retention_loop(self) -> None:
        await asyncio.sleep(60)  # let the service settle first
        while True:
            try:
                await self._purge()
            except Exception:
                log.exception("mail_retention_failed")
            await asyncio.sleep(self._retention)
