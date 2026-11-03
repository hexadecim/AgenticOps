"""Bulkhead: caps concurrent calls to one dependency so it cannot exhaust the caller."""

from __future__ import annotations

import asyncio
from types import TracebackType

from .errors import BulkheadFullError


class Bulkhead:
    def __init__(self, name: str, size: int, wait: float) -> None:
        self.name = name
        self._semaphore = asyncio.Semaphore(size)
        self._wait = wait

    async def __aenter__(self) -> Bulkhead:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._wait)
        except TimeoutError as exc:
            raise BulkheadFullError(self.name) from exc
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._semaphore.release()
