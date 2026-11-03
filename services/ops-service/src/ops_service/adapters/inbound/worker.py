"""Runs approved fixes (at once when woken by an approval, else every few seconds) and keeps the runbook
index current (at start-up, after settings changes, and every few minutes while it is stale)."""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

from ...application import ActiveSettings, RunbookLibrary, RunDueFixes

log = structlog.get_logger(__name__)


class OpsWorker:
    def __init__(
        self,
        run_fixes: RunDueFixes,
        library: RunbookLibrary,
        settings: ActiveSettings,
        *,
        interval: float = 5.0,
        reindex_every: float = 300.0,
    ) -> None:
        self._run, self._library, self._settings = run_fixes, library, settings
        self._interval, self._reindex_every = interval, reindex_every
        self._wake = asyncio.Event()
        self._reindex = True
        self._last_index = 0.0
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    def reindex(self) -> None:
        self._reindex = True
        self._wake.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="ops-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _index(self) -> None:
        stale = not self._library.semantic and time.monotonic() - self._last_index > self._reindex_every
        if not (self._reindex or stale):
            return
        force, self._reindex, self._last_index = self._reindex, False, time.monotonic()
        try:
            await self._library.refresh(await self._settings.get(), force=force)
        except Exception:
            log.exception("runbook_index_failed")

    async def _loop(self) -> None:
        while True:
            try:
                while await self._run():
                    pass
            except Exception:
                log.exception("ops_worker_crashed")
            await self._index()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
