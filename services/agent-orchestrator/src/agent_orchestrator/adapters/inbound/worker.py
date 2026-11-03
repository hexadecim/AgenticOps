"""Moves runs forward: at start-up (recovering interrupted runs), on wake-up, and every few seconds."""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ...application import AdvanceRuns

log = structlog.get_logger(__name__)


class RunWorker:
    def __init__(self, advance: AdvanceRuns, *, interval: float = 5.0) -> None:
        self._advance, self._interval = advance, interval
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        recovered = await self._advance.recover()
        if recovered:
            log.info("runs_recovered", count=recovered)
        self._task = asyncio.create_task(self._loop(), name="agent-runs")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                while await self._advance():  # keep going while there is due work
                    pass
            except Exception:
                log.exception("run_worker_crashed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
