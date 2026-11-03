"""Small infrastructure adapters: wall clock and the mail-provider circuit state."""

from __future__ import annotations

from datetime import UTC, datetime

from opspilot_common.resilience import Resilient


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class BreakerProbe:
    def __init__(self, resilient: Resilient) -> None:
        self._breaker = resilient.breaker

    async def state(self) -> tuple[str, float | None]:
        return (await self._breaker.state()).value, await self._breaker.retry_after()
