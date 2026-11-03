"""Runs approved fixes (at once when woken by an approval, else every few seconds), keeps the runbook
index current (at start-up, after settings changes, and every few minutes while it is stale) and sweeps the
health probes on a fixed interval, so the dashboard's Platform health follows the systems even when no
incident is being diagnosed (`probe.status` is still published only on a change)."""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

from ...application import ActiveSettings, CheckHealth, RunbookLibrary, RunDueFixes

log = structlog.get_logger(__name__)


class OpsWorker:
    def __init__(
        self,
        run_fixes: RunDueFixes,
        library: RunbookLibrary,
        settings: ActiveSettings,
        check_health: CheckHealth | None = None,
        *,
        interval: float = 5.0,
        reindex_every: float = 300.0,
        probe_every: float = 60.0,
    ) -> None:
        self._run, self._library, self._settings = run_fixes, library, settings
        self._check_health = check_health
        self._interval, self._reindex_every, self._probe_every = interval, reindex_every, probe_every
        self._last_probe: float | None = None
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

    async def _probe(self) -> None:
        now = time.monotonic()
        if self._check_health is None or self._probe_every <= 0:
            return
        if self._last_probe is not None and now - self._last_probe < self._probe_every:
            return
        first, self._last_probe = self._last_probe is None, now
        try:
            await self._check_health(None, announce=first)
        except Exception:
            log.exception("probe_sweep_failed")  # evidence unavailable: next sweep tries again

    async def _loop(self) -> None:
        while True:
            try:
                while await self._run():
                    pass
            except Exception:
                log.exception("ops_worker_crashed")
            await self._index()
            await self._probe()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
