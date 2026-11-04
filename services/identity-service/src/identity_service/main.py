"""identity-service ASGI app: `uvicorn --factory identity_service.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opspilot_common.events import EventRuntime
from opspilot_common.logging import configure_logging
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.http import build_router, install_error_handlers
from .bootstrap import Container, build_container
from .config import Settings
from .domain.errors import WeakPasswordError

log = structlog.get_logger(__name__)


def create_app(container: Container | None = None, *, with_events: bool = True) -> FastAPI:
    if container is None:
        settings = Settings()
        configure_logging("identity-service", json=settings.log_json)
        setup_tracing("identity-service")
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = None
        if with_events:
            runtime = EventRuntime("identity-service", container.settings.rabbitmq_url, container.registry)
            runtime.add_outbox(container.sessions, container.tables.outbox)
            await runtime.start()
        s = container.settings
        if s.bootstrap_admin_email and s.bootstrap_admin_password:
            try:
                created = await container.ensure_bootstrap_admin(
                    s.bootstrap_admin_email, s.bootstrap_admin_password, s.bootstrap_admin_name
                )
            except WeakPasswordError as exc:  # fail fast: never create the first admin with a weak password
                raise SystemExit(f"BOOTSTRAP_ADMIN_PASSWORD rejected: {exc.message}") from exc
            if created:
                log.warning("bootstrap_admin_created", email=s.bootstrap_admin_email)
        yield
        if runtime:
            await runtime.stop()
        await container.engine.dispose()

    app = FastAPI(
        telemetry=NO_NATIVE_TELEMETRY, title="OpsPilot identity-service", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(build_router(container))
    app.include_router(health_routes(container.registry))
    return app
