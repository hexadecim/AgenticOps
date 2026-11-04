"""Every breaker in deploy/resilience.yaml against real faults: Toxiproxy between a real client and server.

Thresholds and retry counts are the configured ones; only waits are shortened (a 180 s timeout, a 120 s
reset), so one dependency takes seconds. The state lives in real Redis, as in production.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from conftest import ROOT, Proxy
from opspilot_common.resilience import (
    CircuitOpenError,
    CircuitState,
    RedisStateStore,
    ResilienceError,
    ResilienceRegistry,
    ResilienceSettings,
    ResilientHttpClient,
    WallClock,
)
from redis.asyncio import Redis

CONFIG: Path = ROOT / "deploy" / "resilience.yaml"
REAL = ResilienceSettings.from_yaml(CONFIG)
# `probe-target-<name>` circuits are made per admin-configured probe, from `defaults`.
DEPENDENCIES = [*sorted(REAL.dependencies), "probe-target-example"]
RESET = 0.4
TIMEOUT = 0.15


def scaled() -> ResilienceSettings:
    def scale(cfg):  # type: ignore[no-untyped-def]
        return cfg.model_copy(
            update={
                "timeout": TIMEOUT,
                "bulkhead_wait": 0.05,
                "breaker": cfg.breaker.model_copy(update={"reset_timeout": RESET}),
                "retry": cfg.retry.model_copy(update={"base_delay": 0.001, "max_delay": 0.002}),
            }
        )

    return ResilienceSettings(
        defaults=scale(REAL.defaults), dependencies={n: scale(c) for n, c in REAL.dependencies.items()}
    )


def registry(redis: Redis) -> ResilienceRegistry:
    return ResilienceRegistry(scaled(), store=RedisStateStore(redis), clock=WallClock(), listeners=())


def client(reg: ResilienceRegistry, name: str, proxy: Proxy) -> ResilientHttpClient:
    return ResilientHttpClient.from_registry(reg, name, base_url=f"http://localhost:{proxy.port}")


async def trip(http: ResilientHttpClient, fail_max: int) -> None:
    for _ in range(fail_max):
        with pytest.raises(ResilienceError) as failure:
            await http.get("/")
        assert not isinstance(failure.value, CircuitOpenError)


async def assert_fails_fast(http: ResilientHttpClient) -> None:
    started = time.monotonic()
    with pytest.raises(CircuitOpenError):
        await http.get("/")
    assert time.monotonic() - started < TIMEOUT, "an open circuit must not wait for the dependency"


async def assert_recovers(http: ResilientHttpClient, reg: ResilienceRegistry, name: str) -> None:
    await asyncio.sleep(RESET + 0.05)
    assert (await http.get("/")).status_code == 200
    assert await reg.get(name).breaker.state() is CircuitState.CLOSED


@pytest.mark.parametrize("name", DEPENDENCIES)
async def test_refused_connections_open_the_circuit_and_recovery_closes_it(
    name: str, http_proxy: Proxy, redis: Redis
) -> None:
    reg = registry(redis)
    http = client(reg, name, http_proxy)
    assert (await http.get("/")).status_code == 200
    await http_proxy.down()
    await trip(http, reg.get(name).config.breaker.fail_max)
    assert await reg.get(name).breaker.state() is CircuitState.OPEN
    await assert_fails_fast(http)
    await http_proxy.up()
    await assert_recovers(http, reg, name)
    await http.aclose()


@pytest.mark.parametrize("name", DEPENDENCIES)
async def test_a_slow_dependency_times_out_and_opens_the_circuit(
    name: str, http_proxy: Proxy, redis: Redis
) -> None:
    reg = registry(redis)
    http = client(reg, name, http_proxy)
    await http_proxy.slow(int(TIMEOUT * 1000 * 4))
    await trip(http, reg.get(name).config.breaker.fail_max)
    await assert_fails_fast(http)
    await http_proxy.normal()
    await assert_recovers(http, reg, name)
    await http.aclose()


async def test_a_failed_half_open_probe_opens_the_circuit_again(http_proxy: Proxy, redis: Redis) -> None:
    reg = registry(redis)
    http = client(reg, "jira", http_proxy)
    await http_proxy.down()
    await trip(http, reg.get("jira").config.breaker.fail_max)
    await asyncio.sleep(RESET + 0.05)
    with pytest.raises(ResilienceError):
        await http.get("/")
    assert await reg.get("jira").breaker.state() is CircuitState.OPEN
    await http.aclose()


async def test_replicas_share_one_circuit_through_redis(http_proxy: Proxy, redis: Redis) -> None:
    first, second = registry(redis), registry(redis)
    await http_proxy.down()
    await trip(client(first, "jira", http_proxy), first.get("jira").config.breaker.fail_max)
    await http_proxy.up()  # the second replica would succeed, but the shared circuit is open
    await assert_fails_fast(client(second, "jira", http_proxy))


async def test_a_4xx_never_trips_the_breaker(http_proxy: Proxy, redis: Redis) -> None:
    reg = registry(redis)
    http = client(reg, "jira", http_proxy)
    for _ in range(reg.get("jira").config.breaker.fail_max + 2):
        assert (await http.get("/no-such-page")).status_code == 404
    assert await reg.get("jira").breaker.state() is CircuitState.CLOSED
    await http.aclose()


async def test_the_bulkhead_refuses_callers_beyond_its_limit(http_proxy: Proxy, redis: Redis) -> None:
    reg = registry(redis)
    limit = reg.get("mail-provider").config.bulkhead
    http = client(reg, "mail-provider", http_proxy)
    await http_proxy.slow(100)
    results = await asyncio.gather(*(http.get("/") for _ in range(limit + 3)), return_exceptions=True)
    await http.aclose()
    assert sum(isinstance(r, httpx.Response) for r in results) >= limit
    assert any(isinstance(r, ResilienceError) for r in results)


async def test_every_call_keeps_working_while_redis_is_down(http_proxy: Proxy, redis_proxy: Proxy) -> None:
    """The breakers' state store must not become the thing that takes every service down."""
    shared = Redis.from_url(f"redis://localhost:{redis_proxy.port}/0", socket_timeout=0.2)
    reg = registry(shared)
    http = client(reg, "jira", http_proxy)
    assert (await http.get("/")).status_code == 200
    await redis_proxy.down()
    assert (await http.get("/")).status_code == 200  # served, with state held locally
    await http_proxy.down()
    await trip(http, reg.get("jira").config.breaker.fail_max)
    await assert_fails_fast(http)  # the circuit still works
    await redis_proxy.up()
    await http_proxy.up()
    await asyncio.sleep(RESET + 0.05)
    assert (await http.get("/")).status_code == 200
    await http.aclose()
    await shared.aclose()
