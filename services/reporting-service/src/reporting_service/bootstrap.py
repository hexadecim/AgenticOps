"""Composition root for reporting-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opspilot_common.auth import AuthKeys, Principal
from opspilot_common.resilience import ResilienceRegistry, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.sql import ReportingTables, SqlUnitOfWork
from .adapters.outbound.subject import SqlReportingSubjectStore
from .application import GetDashboard, GetIncidentTimeline, ProjectEvent
from .config import Settings
from .ports import Clock, UnitOfWork

SERVICE = "reporting-service"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: ReportingTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    project: ProjectEvent
    get_dashboard: GetDashboard
    get_timeline: GetIncidentTimeline
    subject_store: SqlReportingSubjectStore
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_privacy: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    clock: Clock | None = None,
) -> Container:
    keys = AuthKeys.load(SERVICE, settings.jwt_secret, settings.auth_keys_dir)
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = ReportingTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables)

    redis = redis or Redis.from_url(settings.redis_url)
    clock = clock or SystemClock()
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=redis_registry(redis, settings.resilience_config),
        project=ProjectEvent(uow),
        get_dashboard=GetDashboard(uow, clock),
        get_timeline=GetIncidentTimeline(uow, clock),
        subject_store=SqlReportingSubjectStore(sessions, tables),
        current_user=principal_dependency(keys.session_verifier()),
        require_privacy=service_dependency(keys.service_verifier(), "privacy-service"),
        closers=[redis.aclose],
    )
