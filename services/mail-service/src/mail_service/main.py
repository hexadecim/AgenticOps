"""mail-service ASGI app: `uvicorn --factory mail_service.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.web import CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import on_settings_changed
from .adapters.inbound.http import build_internal_router, build_router, install_error_handlers
from .bootstrap import SERVICE, Container, build_container
from .config import Settings


def create_app(
    container: Container | None = None, *, with_events: bool = True, with_scheduler: bool = True
) -> FastAPI:
    if container is None:
        settings = Settings()
        configure_logging(SERVICE, json=settings.log_json)
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = None
        if with_events:
            runtime = EventRuntime(SERVICE, container.settings.rabbitmq_url, container.registry)
            runtime.add_outbox(container.sessions, container.tables.outbox)
            inbox = Inbox(container.tables.inbox, f"{SERVICE}.settings")
            handler = on_settings_changed(container.apply_settings, container.scheduler.wake)
            runtime.add_consumer("settings", ["settings.changed"], inbox.guard(container.sessions, handler))
            await runtime.start()
        scheduling = with_scheduler and container.settings.poller_enabled
        if scheduling:
            await container.scheduler.start()
        yield
        if scheduling:
            await container.scheduler.stop()
        if runtime:
            await runtime.stop()
        for close in container.closers:
            await close()
        await container.engine.dispose()

    app = FastAPI(title="OpsPilot mail-service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(build_router(container))
    app.include_router(build_internal_router(container))
    app.include_router(health_routes(container.registry))
    return app
