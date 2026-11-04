"""api-gateway ASGI app: `uvicorn --factory api_gateway.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opspilot_common.events import EventRuntime
from opspilot_common.logging import configure_logging
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.http import (
    CSRF_HEADER,
    CsrfMiddleware,
    SecurityHeadersMiddleware,
    build_router,
    install_error_handlers,
)
from .adapters.inbound.live import RELAYED
from .bootstrap import Container, build_container
from .config import Settings


def create_app(container: Container | None = None) -> FastAPI:
    if container is None:
        settings = Settings()
        configure_logging("api-gateway", json=settings.log_json)
        setup_tracing("api-gateway")
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = None
        if container.settings.live_events:
            runtime = EventRuntime("api-gateway", container.settings.rabbitmq_url, container.registry)
            runtime.add_live_consumer("live", RELAYED, container.live.relay)
            await runtime.start()
        yield
        if runtime:
            await runtime.stop()
        for close in container.closers:
            await close()

    app = FastAPI(
        telemetry=NO_NATIVE_TELEMETRY,
        title="OpsPilot api-gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=container.settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", CSRF_HEADER],
    )
    install_error_handlers(app)
    app.include_router(
        build_router(
            container.identity,
            container.sign_in,
            container.settings_api,
            container.mail,
            container.audit,
            container.privacy,
            container.approvals,
            container.runs,
            container.reporting,
            container.ops,
            container.live,
            container.verifier,
            container.health,
            container.require_admin,
            cookie_secure=container.settings.cookie_secure,
        )
    )
    app.include_router(
        health_routes(container.registry, critical=frozenset({"identity-service"})), prefix="/api"
    )
    return app
