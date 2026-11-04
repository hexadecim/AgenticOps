"""Composition root for approval-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opspilot_common.auth import Principal, ServiceTokenVerifier, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.sql import ApprovalTables, SqlUnitOfWork
from .adapters.outbound.subject import SqlApprovalSubjectStore
from .application import (
    CheckDraft,
    Decide,
    GetApproval,
    ListApprovals,
    RecordFixHealth,
    RecordFixOutcome,
    RecordMessageOutcome,
    RecordRequest,
    RecordTicketOutcome,
    SendReminders,
)
from .config import Settings
from .ports import Clock, UnitOfWork

SERVICE = "approval-service"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: ApprovalTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    record_request: RecordRequest
    record_outcome: RecordTicketOutcome
    record_fix: RecordFixOutcome
    record_health: RecordFixHealth
    record_message: RecordMessageOutcome
    check_draft: CheckDraft
    send_reminders: SendReminders
    decide: Decide
    list_approvals: ListApprovals
    get_approval: GetApproval
    subject_store: SqlApprovalSubjectStore
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
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = ApprovalTables(schema=settings.db_schema if schema is None else schema or None)
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
        record_request=RecordRequest(uow, clock),
        record_outcome=RecordTicketOutcome(uow),
        record_fix=RecordFixOutcome(uow),
        record_health=RecordFixHealth(uow),
        record_message=RecordMessageOutcome(uow),
        check_draft=CheckDraft(uow),
        send_reminders=SendReminders(uow, clock),
        decide=Decide(uow, clock),
        list_approvals=ListApprovals(uow, clock),
        get_approval=GetApproval(uow),
        subject_store=SqlApprovalSubjectStore(sessions, tables),
        current_user=principal_dependency(TokenVerifier(settings.jwt_secret)),
        require_privacy=service_dependency(ServiceTokenVerifier(settings.jwt_secret), "privacy-service"),
        closers=[redis.aclose],
    )
