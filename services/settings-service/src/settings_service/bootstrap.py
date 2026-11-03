"""Composition root for settings-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opspilot_common.auth import Principal, Role, ServiceTokenVerifier, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency, require_roles, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.security import SystemClock
from .adapters.outbound.sql import SecretBox, SettingsTables, SqlUnitOfWork
from .adapters.outbound.testers.gmail import GmailTester
from .adapters.outbound.testers.graph import GraphTester
from .adapters.outbound.testers.imap import ImapTester
from .adapters.outbound.testers.jira import JiraTester
from .adapters.outbound.testers.ollama import OllamaTester
from .adapters.outbound.testers.ops import OpsTester
from .application import GetSection, ResolveSection, RunConnectionTest, SaveSection
from .config import Settings
from .ports import Clock, ConnectionTester, UnitOfWork


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: SettingsTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    get_section: GetSection
    save_section: SaveSection
    run_connection_test: RunConnectionTest
    resolve_section: ResolveSection
    require_admin: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_service: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def default_testers(registry: ResilienceRegistry) -> tuple[dict[str, ConnectionTester], list]:  # type: ignore[type-arg]
    """Connection tests have their own `probe-*` circuits so admin experiments never trip production ones."""

    def http(name: str) -> ResilientHttpClient:
        return ResilientHttpClient.from_registry(registry, name, follow_redirects=False)

    jira, google, microsoft, ollama, ops = (
        http("probe-jira"),
        http("probe-google"),
        http("probe-microsoft"),
        http("probe-ollama"),
        http("probe-ops"),
    )
    testers: dict[str, ConnectionTester] = {
        "mailbox:imap": ImapTester(registry.get("probe-mail")),
        "mailbox:gmail": GmailTester(google),
        "mailbox:graph": GraphTester(microsoft),
        "jira": JiraTester(jira),
        "llm": OllamaTester(ollama),
        "ops": OpsTester(ops),
    }
    return testers, [c.aclose for c in (jira, google, microsoft, ollama, ops)]


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
    schema: str | None = None,
    testers: dict[str, ConnectionTester] | None = None,
    redis: Redis | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = SettingsTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    box = SecretBox(settings.fernet_key)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables, box)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    if testers is None:
        testers, more = default_testers(registry)
        closers.extend(more)
    current = principal_dependency(TokenVerifier(settings.jwt_secret))
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        get_section=GetSection(uow),
        save_section=SaveSection(uow, clock or SystemClock()),
        run_connection_test=RunConnectionTest(uow, testers),
        resolve_section=ResolveSection(uow),
        require_admin=require_roles(current, Role.ADMIN),
        # Any valid service token gets in; ResolveSection checks which service may read which section.
        require_service=service_dependency(ServiceTokenVerifier(settings.jwt_secret)),
        closers=closers,
    )
