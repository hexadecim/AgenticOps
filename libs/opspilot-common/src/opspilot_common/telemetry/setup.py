"""Installs the OpenTelemetry SDK for one service process. Tracing stays off without an endpoint."""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(service: str, endpoint: str | None = None) -> bool:
    """Export spans to `endpoint` (default: OTEL_EXPORTER_OTLP_ENDPOINT). The sampler follows the standard
    OTEL_TRACES_SAMPLER variables. True when tracing was switched on."""
    endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return True
