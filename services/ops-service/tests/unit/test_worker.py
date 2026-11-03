"""The worker sweeps the health probes on its own interval, so Platform health follows the systems even when
no incident is being diagnosed."""

from __future__ import annotations

from typing import Any, cast

from ops_service.adapters.inbound.worker import OpsWorker
from ops_service.application import CheckHealth


class CountingHealth:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str | None] = []
        self.announced: list[bool] = []
        self._fail = fail

    async def __call__(self, region: str | None, *, announce: bool = False, **_: Any) -> list[Any]:
        self.calls.append(region)
        self.announced.append(announce)
        if self._fail:
            raise RuntimeError("evidence unavailable")
        return []


def worker(health: CountingHealth | None, every: float) -> OpsWorker:
    nothing = cast(Any, None)
    return OpsWorker(nothing, nothing, nothing, cast(CheckHealth, health), probe_every=every)


async def test_sweeps_every_region_once_per_interval() -> None:
    health = CountingHealth()
    w = worker(health, every=60)
    await w._probe()
    await w._probe()  # within the interval: skipped
    assert health.calls == [None]  # None = all regions
    assert health.announced == [True]  # the first sweep announces every probe's current state
    w._last_probe = -1e9  # interval passed
    await w._probe()
    assert health.announced == [True, False]  # later sweeps: changes only


async def test_zero_interval_or_no_health_check_never_sweeps() -> None:
    health = CountingHealth()
    await worker(health, every=0)._probe()
    await worker(None, every=60)._probe()
    assert health.calls == []


async def test_a_failed_sweep_is_logged_not_raised() -> None:
    health = CountingHealth(fail=True)
    await worker(health, every=60)._probe()
    assert health.calls == [None]
