"""privacy-service ASGI app: `uvicorn --factory privacy_service.main:create_app`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.web import CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import BINDINGS, on_event
from .adapters.inbound.http import build_internal_router, build_router, install_error_handlers
from .bootstrap import SERVICE, Container, build_container
from .config import Settings

log = structlog.get_logger(__name__)
PURGE_INTERVAL = 3600.0


async def _purge_loop(container: Container) -> None:
    await asyncio.sleep(120)  # let the other services come up: they are asked before anything goes
    while True:
        try:
            purged = await container.purge_identities()
            if purged:
                log.info("vault_purged", subjects=purged)
        except Exception:
            log.exception("vault_purge_failed")
        await asyncio.sleep(PURGE_INTERVAL)


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
            inbox = Inbox(container.tables.inbox, f"{SERVICE}.events")
            handler = on_event(container.record_store_erased, container.record_dsr_message)
            runtime.add_consumer("events", BINDINGS, inbox.guard(container.sessions, handler))
            await runtime.start()
        purge = None
        if with_events and container.settings.vault_purge_enabled:
            purge = asyncio.create_task(_purge_loop(container), name="vault-retention")
        yield
        if purge:
            purge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await purge
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
