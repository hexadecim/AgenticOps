"""ops-service ASGI app: `uvicorn --factory ops_service.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import BINDINGS, on_event
from .adapters.inbound.http import build_internal_router, build_router, install_error_handlers
from .bootstrap import SERVICE, Container, build_container
from .config import Settings

log = structlog.get_logger(__name__)


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
        seeded = await container.world.seed()
        if seeded:
            log.info("mock_faults_opened", count=seeded)
        runtime = None
        if with_events:
            runtime = EventRuntime(SERVICE, container.settings.rabbitmq_url, container.registry)
            runtime.add_outbox(container.sessions, container.tables.outbox)
            inbox = Inbox(container.tables.inbox, f"{SERVICE}.events")
            handler = on_event(
                container.store_proposal,
                container.queue_fix,
                container.active,
                container.worker.wake,
                container.worker.reindex,
            )
            runtime.add_consumer("events", BINDINGS, inbox.guard(container.sessions, handler))
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
        telemetry=NO_NATIVE_TELEMETRY, title="OpsPilot ops-service", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(build_internal_router(container))
    app.include_router(build_router(container))
    app.include_router(health_routes(container.registry))
    return app
