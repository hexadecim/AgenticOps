"""Composition root for audit-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opspilot_common.auth import Principal, ServiceTokenVerifier, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.sql import AuditTables, SqlAuditReader
from .adapters.outbound.subject import SqlAuditSubjectReader
from .application import RecordEvent, SearchAudit
from .config import Settings

SERVICE = "audit-service"


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: AuditTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    record: RecordEvent
    search: SearchAudit
    subject_reader: SqlAuditSubjectReader
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_privacy: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = AuditTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        record=RecordEvent(),
        search=SearchAudit(SqlAuditReader(sessions, tables)),
        subject_reader=SqlAuditSubjectReader(sessions, tables),
        current_user=principal_dependency(TokenVerifier(settings.jwt_secret)),
        require_privacy=service_dependency(ServiceTokenVerifier(settings.jwt_secret), "privacy-service"),
        closers=[redis.aclose],
    )
