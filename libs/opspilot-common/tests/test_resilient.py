import asyncio

import httpx
import pytest
import respx
from opspilot_common.resilience import (
    BulkheadFullError,
    CircuitOpenError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    DependencyUnreachableError,
    ResilienceRegistry,
    ResilienceSettings,
    ResilientHttpClient,
)

SETTINGS = ResilienceSettings.from_mapping(
    {
        "defaults": {
            "timeout": 0.2,
            "bulkhead": 2,
            "bulkhead_wait": 0.01,
            "retry": {"attempts": 3, "base_delay": 0, "max_delay": 0},
        },
        "dependencies": {"jira": {"breaker": {"fail_max": 2}}},
    }
)


def test_partial_override_keeps_defaults() -> None:
    jira = SETTINGS.for_dependency("jira")
    assert jira.breaker.fail_max == 2
    assert jira.breaker.reset_timeout == 30
    assert jira.retry.attempts == 3
    assert SETTINGS.for_dependency("unknown").breaker.fail_max == 5


async def test_retries_transient_then_succeeds() -> None:
    resilient = ResilienceRegistry(SETTINGS).get("ollama")
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("refused")
        return "ok"

    assert await resilient.call(flaky) == "ok"
    assert attempts == 3


async def test_no_retry_for_non_idempotent() -> None:
    resilient = ResilienceRegistry(SETTINGS).get("ollama")
    attempts = 0

    async def fail() -> str:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        await resilient.call(fail, retry=False)
    assert attempts == 1


async def test_timeout_is_counted_and_reported() -> None:
    resilient = ResilienceRegistry(SETTINGS).get("jira")

    async def hang() -> str:
        await asyncio.sleep(5)
        return "never"

    for _ in range(2):
        with pytest.raises(DependencyTimeoutError):
            await resilient.call(hang, retry=False)
    with pytest.raises(CircuitOpenError):
        await resilient.call(hang)


async def test_fallback_receives_open_circuit() -> None:
    resilient = ResilienceRegistry(SETTINGS).get("jira")

    async def fail() -> str:
        raise DependencyUnavailableError("jira", 502)

    for _ in range(2):
        with pytest.raises(DependencyUnavailableError):
            await resilient.call(fail)

    async def queued(exc: Exception) -> str:
        return f"queued:{type(exc).__name__}"

    assert await resilient.call_with_fallback(fail, queued) == "queued:CircuitOpenError"


async def test_bulkhead_rejects_excess_concurrency() -> None:
    resilient = ResilienceRegistry(SETTINGS).get("ops")
    release = asyncio.Event()

    async def held() -> str:
        await release.wait()
        return "ok"

    holders = [asyncio.create_task(resilient.call(held)) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(BulkheadFullError):
        await resilient.call(held)
    release.set()
    assert await asyncio.gather(*holders) == ["ok", "ok"]


@respx.mock
async def test_http_client_maps_5xx_and_passes_4xx() -> None:
    registry = ResilienceRegistry(SETTINGS)
    client = ResilientHttpClient.from_registry(registry, "jira", base_url="https://jira.example")
    route = respx.get("https://jira.example/issue/KAN-1").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"key": "KAN-1"})]
    )
    response = await client.get("/issue/KAN-1")
    assert response.json() == {"key": "KAN-1"}
    assert route.call_count == 3

    respx.post("https://jira.example/issue").mock(return_value=httpx.Response(400, json={"errors": {}}))
    for _ in range(5):
        assert (await client.post("/issue", json={})).status_code == 400
    assert (await registry.states())["jira"].value == "closed"
    await client.aclose()


@respx.mock
async def test_http_post_retried_only_with_idempotency_key() -> None:
    client = ResilientHttpClient.from_registry(ResilienceRegistry(SETTINGS), "ops", base_url="https://ops")
    route = respx.post("https://ops/actions").mock(return_value=httpx.Response(500))
    with pytest.raises(DependencyUnavailableError):
        await client.post("/actions")
    assert route.call_count == 1
    with pytest.raises(DependencyUnavailableError):
        await client.post("/actions", idempotency_key="appr-1")
    assert route.call_count == 4
    assert route.calls.last.request.headers["Idempotency-Key"] == "appr-1"
    await client.aclose()


@respx.mock
async def test_http_transport_errors_surface_as_resilience_errors() -> None:
    client = ResilientHttpClient.from_registry(
        ResilienceRegistry(SETTINGS), "identity", base_url="https://id"
    )
    respx.get("https://id/me").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DependencyUnreachableError):
        await client.get("/me")
    respx.get("https://id/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(DependencyTimeoutError):
        await client.get("/slow")
    await client.aclose()
