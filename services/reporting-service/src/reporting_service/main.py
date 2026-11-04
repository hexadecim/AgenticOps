"""reporting-service ASGI app: `uvicorn --factory reporting_service.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.subjects_http import ERASURE_BINDINGS, erasure_handler, subject_router
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import on_any_event
from .adapters.inbound.http import build_router
from .bootstrap import SERVICE, Container, build_container
from .config import Settings


def create_app(container: Container | None = None, *, with_events: bool = True) -> FastAPI:
    if container is None:
        settings = Settings()
        configure_logging(SERVICE, json=settings.log_json)
        setup_tracing(SERVICE)
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = None
        if with_events:
            runtime = EventRuntime(SERVICE, container.settings.rabbitmq_url, container.registry)
            runtime.add_outbox(container.sessions, container.tables.outbox)
            inbox = Inbox(container.tables.inbox, f"{SERVICE}.all")
            handler = on_any_event(container.sessions, container.tables)
            runtime.add_consumer("all", ["#"], inbox.guard(container.sessions, handler))
            runtime.add_consumer(
                "subjects",
                ERASURE_BINDINGS,
                Inbox(container.tables.inbox, f"{SERVICE}.subjects").guard(
                    container.sessions, erasure_handler(container.subject_store)
                ),
            )
            await runtime.start()
        yield
        if runtime:
            await runtime.stop()
        for close in container.closers:
            await close()
        await container.engine.dispose()

    app = FastAPI(
        telemetry=NO_NATIVE_TELEMETRY, title="OpsPilot reporting-service", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(build_router(container))
    app.include_router(subject_router(container.subject_store, container.require_privacy))
    app.include_router(health_routes(container.registry))
    return app
