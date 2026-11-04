"""agent-orchestrator ASGI app: `uvicorn --factory agent_orchestrator.main:create_app`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from opspilot_common.events import EventRuntime, Inbox
from opspilot_common.logging import configure_logging
from opspilot_common.subjects_http import ERASURE_BINDINGS, erasure_handler, subject_router
from opspilot_common.telemetry.setup import setup_tracing
from opspilot_common.web import NO_NATIVE_TELEMETRY, CorrelationIdMiddleware, health_routes

from .adapters.inbound.events import BINDINGS, on_event
from .adapters.inbound.http import build_router
from .bootstrap import SERVICE, Container, build_container
from .config import Settings


async def postgres_checkpointer(stack: AsyncExitStack, url: str) -> Any:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        url,
        max_size=4,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    stack.push_async_callback(pool.close)
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()  # creates/migrates checkpoint tables in the orchestrator schema
    return saver


def create_app(
    container: Container | None = None, *, with_events: bool = True, checkpointer: Any = None
) -> FastAPI:
    if container is None:
        settings = Settings()
        configure_logging(SERVICE, json=settings.log_json)
        setup_tracing(SERVICE)
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            saver = checkpointer or await postgres_checkpointer(stack, container.settings.checkpoint_url)
            worker = container.runner(saver)[1]
            container.subject_store.use_checkpointer(saver)
            runtime = None
            if with_events:
                runtime = EventRuntime(SERVICE, container.settings.rabbitmq_url, container.registry)
                runtime.add_outbox(container.sessions, container.tables.outbox)
                inbox = Inbox(container.tables.inbox, f"{SERVICE}.events")
                handler = on_event(
                    container.start_run, container.record_decision, container.record_ticket,
                    container.llm_settings, worker.wake, container.record_fix,
                    container.record_message,
                )  # fmt: skip
                runtime.add_consumer("events", BINDINGS, inbox.guard(container.sessions, handler))
                runtime.add_consumer(
                    "subjects",
                    ERASURE_BINDINGS,
                    Inbox(container.tables.inbox, f"{SERVICE}.subjects").guard(
                        container.sessions, erasure_handler(container.subject_store)
                    ),
                )
                await runtime.start()
            if container.settings.worker_enabled:
                await worker.start()
            yield
            await worker.stop()
            if runtime:
                await runtime.stop()
        for close in container.closers:
            await close()
        await container.engine.dispose()

    app = FastAPI(
        telemetry=NO_NATIVE_TELEMETRY, title="OpsPilot agent-orchestrator", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(build_router(container))
    app.include_router(subject_router(container.subject_store, container.require_privacy))
    app.include_router(health_routes(container.registry))
    return app
