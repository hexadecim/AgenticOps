"""Composition root for privacy-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opspilot_common.auth import Principal, ServiceTokenIssuer, ServiceTokenVerifier, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.crypto import VaultCrypto
from .adapters.outbound.detectors import CompositeDetector
from .adapters.outbound.detectors.regex import RegexDetector
from .adapters.outbound.mail_http import MailServiceSource
from .adapters.outbound.sql import PrivacyTables, SqlUnitOfWork
from .adapters.outbound.system import SystemClock
from .application import Detokenise, PseudonymiseEmail, RevealIdentity
from .config import Settings
from .domain.records import DETOKENISE_SERVICES, PSEUDONYMISE_SERVICES
from .ports import Clock, EntityDetector, RawEmailSource, UnitOfWork

SERVICE = "privacy-service"


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: PrivacyTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    pseudonymise: PseudonymiseEmail
    reveal: RevealIdentity
    detokenise: Detokenise
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_service: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def default_detector(settings: Settings) -> EntityDetector:
    detectors: list[EntityDetector] = [RegexDetector()]
    if settings.ner_enabled:
        from .adapters.outbound.detectors.presidio import PresidioDetector, build_engine

        detectors.append(PresidioDetector(build_engine()))
    return CompositeDetector(detectors)


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    source: RawEmailSource | None = None,
    detector: EntityDetector | None = None,
    clock: Clock | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = PrivacyTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    crypto = VaultCrypto(settings.fernet_key)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables, crypto)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    if source is None:
        http = ResilientHttpClient.from_registry(registry, "mail-service", base_url=settings.mail_url)
        closers.append(http.aclose)
        source = MailServiceSource(http, ServiceTokenIssuer(settings.jwt_secret, SERVICE))
    clock = clock or SystemClock()
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        pseudonymise=PseudonymiseEmail(
            uow, source, detector or default_detector(settings), RegexDetector(), clock
        ),
        reveal=RevealIdentity(uow, clock),
        detokenise=Detokenise(uow),
        current_user=principal_dependency(TokenVerifier(settings.jwt_secret)),
        # Use cases decide which service may do what; the route only requires a valid service token.
        require_service=service_dependency(
            ServiceTokenVerifier(settings.jwt_secret), *sorted(PSEUDONYMISE_SERVICES | DETOKENISE_SERVICES)
        ),
        closers=closers,
    )
