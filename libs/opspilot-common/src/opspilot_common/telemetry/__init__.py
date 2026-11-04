"""Tracing helpers on the OpenTelemetry API only: without `setup_tracing` everything here is a no-op.

One rule keeps personal data out of traces: spans carry names, ids and status codes. No URLs, query strings,
SQL, exception messages or email text, because a trace store has no erasure and no retention job.

A case is one trace: the traceparent travels in event envelopes and is stored on queued work, so it survives
the bus and a background worker. Inside a trace the correlation id is the trace id, so an audit entry's
correlation id finds its trace in Jaeger.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import Token

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ..logging import correlation_id

_tracer = trace.get_tracer("opspilot")
_propagator = TraceContextTextMapPropagator()

__all__ = ["SpanKind", "current_traceparent", "inject_traceparent", "span"]


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


def inject_traceparent(headers: MutableMapping[str, str]) -> None:
    _propagator.inject(headers)


@contextmanager
def span(
    name: str,
    *,
    traceparent: str | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    correlation: str | None = None,
    expected: tuple[type[BaseException], ...] = (),
    **attributes: str | int | bool,
) -> Iterator[Span]:
    """Start a span, as a child of `traceparent` when given (work picked up from a queue or the bus).

    Failures record the exception's class only: its message may quote a customer's data. `expected` lists
    exceptions that are control flow, not failures (a graph stopping at an approval gate)."""
    context = _propagator.extract({"traceparent": traceparent}) if traceparent else None
    with _tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        reset = _bind_correlation(current, correlation, joined=traceparent is not None)
        try:
            yield current
        except expected:
            raise
        except BaseException as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        finally:
            if reset is not None:
                correlation_id.reset(reset)


def _bind_correlation(current: Span, explicit: str | None, *, joined: bool) -> Token[str | None] | None:
    value = explicit
    if value is None and (joined or correlation_id.get() is None):
        context = current.get_span_context()
        value = format(context.trace_id, "032x") if context.is_valid else None
    return correlation_id.set(value) if value else None
