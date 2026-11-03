"""Sends due reminders every minute (T2 waits for a named support agent; remind every 15 minutes)."""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ...application import SendReminders

log = structlog.get_logger(__name__)


class ReminderLoop:
    def __init__(self, send: SendReminders, *, interval: float = 60.0) -> None:
        self._send, self._interval = send, interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="approval-reminders")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                sent = await self._send()
                if sent:
                    log.info("approval_reminders_sent", count=sent)
            except Exception:
                log.exception("approval_reminders_failed")
            await asyncio.sleep(self._interval)
