"""Traces follow a case across HTTP, the bus and queued work, and carry no personal data."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from opspilot_common.events.envelope import EventEnvelope
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient
from opspilot_common.telemetry import current_traceparent, span
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def spans() -> Iterator[InMemorySpanExporter]:
    _exporter.clear()
    yield _exporter


def _trace_id(traceparent: str | None) -> str:
    assert traceparent
    return traceparent.split("-")[1]


def test_envelope_carries_the_trace_and_a_consumer_continues_it(spans: InMemorySpanExporter) -> None:
    with span("origin"):
        origin = current_traceparent()
        envelope = EventEnvelope(type="x", source="s")
    assert envelope.traceparent == origin
    received = EventEnvelope.from_bytes(envelope.to_bytes())
    assert received.traceparent == origin  # not replaced by the consumer's own context
    with span("consume", traceparent=received.traceparent):
        assert _trace_id(current_traceparent()) == _trace_id(origin)


def test_inside_a_trace_the_correlation_id_is_the_trace_id() -> None:
    with span("origin"):
        origin = current_traceparent()
    assert correlation_id.get() is None
    with span("worker", traceparent=origin):
        assert correlation_id.get() == _trace_id(origin)
    assert correlation_id.get() is None


def test_a_child_span_keeps_the_correlation_id_already_set() -> None:
    with span("outer", correlation="given"), span("inner"):
        assert correlation_id.get() == "given"


def test_a_failure_records_the_class_never_the_message(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError), span("step"):
        raise ValueError("jane.doe@example.com is broken")
    finished = spans.get_finished_spans()[-1]
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.status.description == "ValueError"
    assert not finished.events
    assert "jane.doe" not in finished.to_json()


async def _call(name: str) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    resilient = ResilienceRegistry().get(name)
    client = ResilientHttpClient(resilient, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.get("http://dependency/path?who=jane")
    return seen[0]


async def test_own_services_join_the_trace_and_third_parties_see_nothing(
    spans: InMemorySpanExporter,
) -> None:
    with span("origin"):
        origin = current_traceparent()
        internal = await _call("mail-service")
        external = await _call("jira")
    assert _trace_id(internal.headers["traceparent"]) == _trace_id(origin)
    assert "traceparent" not in external.headers
    assert all("who=jane" not in s.to_json() for s in spans.get_finished_spans())


async def _app_get(path: str, **headers: str) -> httpx.Response:
    app = FastAPI(telemetry=NO_NATIVE_TELEMETRY)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/v1/things/{thing}")
    async def thing(thing: str) -> dict[str, str | None]:
        return {"correlation": correlation_id.get(), "trace": current_traceparent()}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path, headers=headers)


async def test_server_span_is_named_by_route_and_joins_the_caller(spans: InMemorySpanExporter) -> None:
    with span("caller"):
        origin = current_traceparent()
    assert origin
    response = await _app_get("/v1/things/jane?q=secret", traceparent=origin)
    assert _trace_id(response.json()["trace"]) == _trace_id(origin)
    assert response.headers["X-Correlation-ID"] == _trace_id(origin)
    server = spans.get_finished_spans()[-1]
    assert server.name == "GET /v1/things/{thing}"
    assert server.attributes and server.attributes["http.status_code"] == 200
    assert "jane" not in server.to_json()
    assert "secret" not in server.to_json()


async def test_health_checks_make_no_spans(spans: InMemorySpanExporter) -> None:
    await _app_get("/health/live")
    assert [s.name for s in spans.get_finished_spans()] == []


@pytest.mark.parametrize("bad", ["x" * 65, "a b", "<script>"])
async def test_an_unsafe_correlation_header_is_replaced(bad: str) -> None:
    response = await _app_get("/v1/things/a", **{"X-Correlation-ID": bad})
    assert response.headers["X-Correlation-ID"] != bad
    assert len(response.headers["X-Correlation-ID"]) <= 64


def test_every_service_turns_off_fastapis_own_telemetry() -> None:
    mains = list((Path(__file__).parents[3] / "services").glob("*/src/*/main.py"))
    assert len(mains) == 11
    assert [m for m in mains if "telemetry=NO_NATIVE_TELEMETRY" not in m.read_text()] == []
