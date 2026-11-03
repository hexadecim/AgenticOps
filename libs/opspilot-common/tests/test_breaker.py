import httpx
import pytest
from opspilot_common.resilience import (
    BreakerConfig,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    DependencyUnavailableError,
)
from opspilot_common.testing import FakeClock


class Recorder:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str]] = []

    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
        self.transitions.append((old.value, new.value))


async def ok() -> str:
    return "ok"


async def server_error() -> str:
    raise DependencyUnavailableError("jira", 503)


async def client_error() -> str:
    request = httpx.Request("POST", "https://jira.example/rest/api/3/issue")
    raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, request=request))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def breaker(clock: FakeClock, recorder: Recorder) -> CircuitBreaker:
    return CircuitBreaker(
        "jira", BreakerConfig(fail_max=3, reset_timeout=30), clock=clock, listeners=[recorder]
    )


async def trip(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        with pytest.raises(DependencyUnavailableError):
            await breaker.call(server_error)


async def test_opens_after_fail_max_and_short_circuits(breaker: CircuitBreaker, recorder: Recorder) -> None:
    await trip(breaker, 3)
    assert await breaker.state() is CircuitState.OPEN
    calls = 0

    async def counted() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    with pytest.raises(CircuitOpenError) as info:
        await breaker.call(counted)
    assert calls == 0
    assert info.value.retry_after == pytest.approx(30)
    assert recorder.transitions == [("closed", "open")]


async def test_success_resets_failure_count(breaker: CircuitBreaker) -> None:
    await trip(breaker, 2)
    assert await breaker.call(ok) == "ok"
    await trip(breaker, 2)
    assert await breaker.state() is CircuitState.CLOSED


async def test_client_errors_never_trip(breaker: CircuitBreaker) -> None:
    for _ in range(10):
        with pytest.raises(httpx.HTTPStatusError):
            await breaker.call(client_error)
    assert await breaker.state() is CircuitState.CLOSED


async def test_half_open_success_closes(
    breaker: CircuitBreaker, clock: FakeClock, recorder: Recorder
) -> None:
    await trip(breaker, 3)
    clock.advance(30)
    assert await breaker.state() is CircuitState.HALF_OPEN
    assert await breaker.call(ok) == "ok"
    assert await breaker.state() is CircuitState.CLOSED
    assert recorder.transitions == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


async def test_half_open_failure_reopens(breaker: CircuitBreaker, clock: FakeClock) -> None:
    await trip(breaker, 3)
    clock.advance(31)
    await trip(breaker, 1)
    assert await breaker.state() is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)


async def test_half_open_allows_limited_probes(breaker: CircuitBreaker, clock: FakeClock) -> None:
    import asyncio

    await trip(breaker, 3)
    clock.advance(30)
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "ok"

    probe = asyncio.create_task(breaker.call(slow))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)
    gate.set()
    assert await probe == "ok"


async def test_broken_listener_does_not_break_calls(clock: FakeClock) -> None:
    class Broken:
        async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
            raise RuntimeError("observer down")

    breaker = CircuitBreaker("x", BreakerConfig(fail_max=1), clock=clock, listeners=[Broken()])
    await trip(breaker, 1)
    assert await breaker.state() is CircuitState.OPEN


async def test_listener_added_later_sees_existing_breakers() -> None:
    from opspilot_common.resilience import ResilienceRegistry, ResilienceSettings

    registry = ResilienceRegistry(ResilienceSettings(), clock=FakeClock())
    resilient = registry.get("jira")  # built before the listener exists (as in every bootstrap)
    recorder = Recorder()
    registry.add_listener(recorder)
    for _ in range(resilient.config.breaker.fail_max):
        with pytest.raises(Exception):  # noqa: B017 - the failure itself is not under test
            await resilient.call(server_error, retry=False)
    assert ("closed", "open") in recorder.transitions


async def test_breaker_events_are_published_only_when_the_bus_is_ready() -> None:
    import asyncio

    from opspilot_common.events import BreakerEventListener, InMemoryEventBus

    bus, ready = InMemoryEventBus(), asyncio.Event()
    listener = BreakerEventListener("agent-orchestrator", bus, ready)
    await listener.on_state_change("ollama", CircuitState.CLOSED, CircuitState.OPEN)
    ready.set()
    await listener.on_state_change("rabbitmq", CircuitState.CLOSED, CircuitState.OPEN)  # never about the bus
    await listener.on_state_change("ollama", CircuitState.OPEN, CircuitState.HALF_OPEN)
    await listener.drain()
    (event,) = bus.published
    assert event.type == "breaker.state_changed" and event.payload == {
        "service": "agent-orchestrator", "dependency": "ollama", "old": "open", "new": "half_open",
    }  # fmt: skip
