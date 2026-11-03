"""Clock abstraction so breaker timing is testable without sleeping."""

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class WallClock:
    """Wall time; required when breaker state is shared across processes (Redis)."""

    def now(self) -> float:
        return time.time()
