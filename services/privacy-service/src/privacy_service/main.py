"""privacy-service ASGI app: `uvicorn --factory privacy_service.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opspilot_common.events import EventRuntime
from opspilot_common.logging import configure_logging
from opspilot_common.web import CorrelationIdMiddleware, health_routes

from .adapters.inbound.http import build_internal_router, build_router, install_error_handlers
from .bootstrap import SERVICE, Container, build_container
from .config import Settings


def create_app(container: Container | None = None, *, with_events: bool = True) -> FastAPI:
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
            await runtime.start()
        yield
        if runtime:
            await runtime.stop()
        for close in container.closers:
            await close()
        await container.engine.dispose()

    app = FastAPI(title="OpsPilot privacy-service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(build_router(container))
    app.include_router(build_internal_router(container))
    app.include_router(health_routes(container.registry, critical=frozenset({"mail-service"})))
    return app
