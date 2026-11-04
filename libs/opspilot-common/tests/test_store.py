import contextlib

import fakeredis.aioredis
from opspilot_common.resilience import (
    BreakerConfig,
    CircuitBreaker,
    CircuitState,
    DependencyUnavailableError,
    RedisStateStore,
)
from opspilot_common.resilience.store import BreakerSnapshot
from opspilot_common.testing import FakeClock


async def test_replicas_share_circuit_state_via_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    clock = FakeClock()
    store = RedisStateStore(redis)
    replica_a = CircuitBreaker("jira", BreakerConfig(fail_max=1), store=store, clock=clock)
    replica_b = CircuitBreaker("jira", BreakerConfig(fail_max=1), store=store, clock=clock)

    async def fail() -> None:
        raise DependencyUnavailableError("jira", 500)

    with contextlib.suppress(DependencyUnavailableError):
        await replica_a.call(fail)
    assert await replica_b.state() is CircuitState.OPEN


class _DownRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, _: str) -> bytes:
        self.calls += 1
        raise ConnectionError("down")

    async def set(self, _: str, __: str) -> None:
        self.calls += 1
        raise ConnectionError("down")


async def test_state_is_kept_locally_while_redis_is_down() -> None:
    redis = _DownRedis()
    store = RedisStateStore(redis, retry_after=60)  # type: ignore[arg-type]
    assert await store.load("jira") == BreakerSnapshot()  # no error reaches the caller
    opened = BreakerSnapshot(state=CircuitState.OPEN, failures=5, opened_at=1.0)
    await store.save("jira", opened)
    assert await store.load("jira") == opened
    assert redis.calls == 1  # not asked again until retry_after: calls must not slow down
