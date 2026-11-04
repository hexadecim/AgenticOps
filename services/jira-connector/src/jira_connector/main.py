"""jira-connector ASGI app: `uvicorn --factory jira_connector.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.subjects_http import ERASURE_BINDINGS, erasure_handler, subject_router
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import BINDINGS, on_event
from .adapters.inbound.http import build_internal_router, install_error_handlers
from .bootstrap import SERVICE, Container, build_container
from .config import Settings


def create_app(
    container: Container | None = None, *, with_events: bool = True, with_worker: bool = True
) -> FastAPI:
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
            inbox = Inbox(container.tables.inbox, f"{SERVICE}.events")
            handler = on_event(
                container.store_proposal, container.queue_job, container.active, container.worker.wake
            )
            runtime.add_consumer("events", BINDINGS, inbox.guard(container.sessions, handler))
            runtime.add_consumer(
                "subjects",
                ERASURE_BINDINGS,
                Inbox(container.tables.inbox, f"{SERVICE}.subjects").guard(
                    container.sessions, erasure_handler(container.subject_store)
                ),
            )
            await runtime.start()
        working = with_worker and container.settings.worker_enabled
        if working:
            await container.worker.start()
        yield
        if working:
            await container.worker.stop()
        if runtime:
            await runtime.stop()
        for close in container.closers:
            await close()
        await container.engine.dispose()

    app = FastAPI(
        telemetry=NO_NATIVE_TELEMETRY, title="OpsPilot jira-connector", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(build_internal_router(container))
    app.include_router(subject_router(container.subject_store, container.require_privacy))
    app.include_router(health_routes(container.registry))
    return app
