"""Runs due Jira jobs every few seconds, or at once when woken by an approval."""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ...application import ProcessDueJobs

log = structlog.get_logger(__name__)


class JobWorker:
    def __init__(self, process: ProcessDueJobs, *, interval: float = 5.0) -> None:
        self._process, self._interval = process, interval
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="jira-jobs")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self._process()
            except Exception:
                log.exception("jira_worker_crashed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
