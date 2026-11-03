"""Composition root for mail-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from opspilot_common.auth import Principal, Role, ServiceTokenIssuer, ServiceTokenVerifier, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency, require_roles, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.inbound.scheduler import Scheduler
from .adapters.outbound.providers import DEPENDENCY, default_registry
from .adapters.outbound.settings_http import SettingsServiceSource
from .adapters.outbound.sql import MailTables, SqlUnitOfWork
from .adapters.outbound.system import BreakerProbe, SystemClock
from .application import (
    ActiveMailbox,
    ApplyMailboxSettings,
    GetEmail,
    GetMailboxStatus,
    PollMailbox,
    PurgeExpiredEmails,
)
from .config import Settings
from .ports import Clock, MailboxBuilder, SettingsSource, UnitOfWork

SERVICE = "mail-service"
# The only service that may read raw (not yet pseudonymised) email content. Everyone else, the agents
# included, gets the tokenised version from privacy-service.
RAW_EMAIL_READERS = ("privacy-service",)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: MailTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    active: ActiveMailbox
    apply_settings: ApplyMailboxSettings
    poll: PollMailbox
    purge: PurgeExpiredEmails
    get_status: GetMailboxStatus
    get_email: GetEmail
    scheduler: Scheduler
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_admin: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_reader_service: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    source: SettingsSource | None = None,
    builder: MailboxBuilder | None = None,
    clock: Clock | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = MailTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    if source is None:
        http = ResilientHttpClient.from_registry(registry, "settings-service", base_url=settings.settings_url)
        closers.append(http.aclose)
        source = SettingsServiceSource(http, ServiceTokenIssuer(settings.jwt_secret, SERVICE))
    clock = clock or SystemClock()
    active = ActiveMailbox()
    apply_settings = ApplyMailboxSettings(active, source, builder or default_registry(registry))
    poll = PollMailbox(
        uow,
        active,
        apply_settings,
        clock,
        batch_size=settings.batch_size,
        initial_lookback=timedelta(hours=settings.initial_lookback_hours),
    )
    purge = PurgeExpiredEmails(uow, clock, days=settings.retention_days)
    current = principal_dependency(TokenVerifier(settings.jwt_secret))

    async def close_mailbox() -> None:
        if active.connection is not None:
            await active.connection.close()

    closers.append(close_mailbox)
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        active=active,
        apply_settings=apply_settings,
        poll=poll,
        purge=purge,
        get_status=GetMailboxStatus(uow, active, BreakerProbe(registry.get(DEPENDENCY)), clock),
        get_email=GetEmail(uow),
        scheduler=Scheduler(poll, purge, active),
        current_user=current,
        require_admin=require_roles(current, Role.ADMIN),
        require_reader_service=service_dependency(
            ServiceTokenVerifier(settings.jwt_secret), *RAW_EMAIL_READERS
        ),
        closers=closers,
    )
