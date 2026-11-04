"""Composition root for ops-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opspilot_common.auth import AuthKeys, Principal
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.inbound.worker import OpsWorker
from .adapters.outbound.files import FileLogs, MarkdownRunbooks
from .adapters.outbound.http import CombinedProbes, OllamaEmbedder, SettingsServiceSource
from .adapters.outbound.mock_world import MockWorld, load_spec
from .adapters.outbound.sql import OpsTables, SqlRunbookIndex, SqlUnitOfWork
from .application import (
    ActiveSettings,
    CheckHealth,
    Diagnose,
    GetEvidence,
    QueueFix,
    RunbookLibrary,
    RunDueFixes,
    StoreFixProposal,
)
from .config import Settings
from .ports import ActionExecutor, Clock, Embedder, LogSource, RunbookSource, SettingsSource, UnitOfWork

SERVICE = "ops-service"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: OpsTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    clock: Clock
    world: MockWorld
    active: ActiveSettings
    library: RunbookLibrary
    check_health: CheckHealth
    diagnose: Diagnose
    get_evidence: GetEvidence
    store_proposal: StoreFixProposal
    queue_fix: QueueFix
    run_fixes: RunDueFixes
    worker: OpsWorker
    require_service: Callable[..., Awaitable[str]] = field(repr=False)
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    source: SettingsSource | None = None,
    embedder: Embedder | None = None,
    spec: dict[str, Any] | None = None,
    logs: LogSource | None = None,
    runbooks: RunbookSource | None = None,
    executors: dict[str, ActionExecutor] | None = None,
    clock: Clock | None = None,
) -> Container:
    keys = AuthKeys.load(SERVICE, settings.jwt_secret, settings.auth_keys_dir)
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = OpsTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    if source is None:
        http = ResilientHttpClient.from_registry(registry, "settings-service", base_url=settings.settings_url)
        closers.append(http.aclose)
        source = SettingsServiceSource(http, keys.service_issuer())
    if embedder is None:
        ollama = OllamaEmbedder(registry)
        closers.append(ollama.aclose)
        embedder = ollama
    clock = clock or SystemClock()
    active = ActiveSettings(source)
    world = MockWorld(
        sessions, tables, spec if spec is not None else load_spec(settings.mock_environment), clock
    )
    probes = CombinedProbes(world, registry, active)
    closers.append(probes.aclose)
    logs = logs or FileLogs()
    library = RunbookLibrary(runbooks or MarkdownRunbooks(), SqlRunbookIndex(sessions, tables), embedder)
    health = CheckHealth(uow, probes, clock)
    # Executors by the name chosen in Admin > Ops. A real one is a new adapter + an entry here (O in SOLID).
    executors = executors if executors is not None else {world.name: world}
    run_fixes = RunDueFixes(uow, active, executors, health, clock)
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        clock=clock,
        world=world,
        active=active,
        library=library,
        check_health=health,
        diagnose=Diagnose(active, world, health, logs, library, clock),
        get_evidence=GetEvidence(uow, active, health, logs, clock),
        store_proposal=StoreFixProposal(uow),
        queue_fix=QueueFix(uow, active, world, clock),
        run_fixes=run_fixes,
        worker=OpsWorker(run_fixes, library, active, health, probe_every=settings.probe_interval_seconds),
        require_service=service_dependency(keys.service_verifier(), "agent-orchestrator"),
        current_user=principal_dependency(keys.session_verifier()),
        closers=closers,
    )
