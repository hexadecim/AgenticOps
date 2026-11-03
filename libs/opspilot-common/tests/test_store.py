import contextlib

import fakeredis.aioredis
from opspilot_common.resilience import (
    BreakerConfig,
    CircuitBreaker,
    CircuitState,
    DependencyUnavailableError,
    RedisStateStore,
)
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
