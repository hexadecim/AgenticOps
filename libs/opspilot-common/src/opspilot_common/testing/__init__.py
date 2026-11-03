"""Test doubles shared by every service's test suite."""

from __future__ import annotations


class FakeClock:
    """Deterministic clock for breaker and timer tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


__all__ = ["FakeClock"]
