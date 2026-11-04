"""Composition root for jira-connector."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opspilot_common.auth import ServiceTokenIssuer, ServiceTokenVerifier
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.inbound.worker import JobWorker
from .adapters.outbound.jira_cloud import JiraCloudTracker
from .adapters.outbound.settings_http import SettingsServiceSource
from .adapters.outbound.sql import JiraTables, SqlUnitOfWork
from .adapters.outbound.subject import SqlJiraSubjectStore
from .application import ActiveTracker, GetContext, ProcessDueJobs, QueueJob, StoreProposal
from .config import Settings
from .domain.model import JiraSettings
from .ports import Clock, JiraTracker, SettingsSource, TrackerFactory, UnitOfWork

SERVICE = "jira-connector"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: JiraTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    active: ActiveTracker
    store_proposal: StoreProposal
    queue_job: QueueJob
    process_jobs: ProcessDueJobs
    get_context: GetContext
    worker: JobWorker
    require_service: Callable[..., Awaitable[str]] = field(repr=False)
    require_privacy: Callable[..., Awaitable[str]] = field(repr=False)
    subject_store: SqlJiraSubjectStore = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def jira_cloud_factory(registry: ResilienceRegistry) -> TrackerFactory:
    def build(settings: JiraSettings) -> JiraTracker:
        http = ResilientHttpClient.from_registry(
            registry,
            "jira",
            base_url=settings.base_url,
            auth=(settings.email, settings.api_token),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        return JiraCloudTracker(settings, http)

    return build


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    source: SettingsSource | None = None,
    factory: TrackerFactory | None = None,
    clock: Clock | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = JiraTables(schema=settings.db_schema if schema is None else schema or None)
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
    active = ActiveTracker(source, factory or jira_cloud_factory(registry))
    process = ProcessDueJobs(uow, active, clock)

    closers.append(active.close)
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        active=active,
        store_proposal=StoreProposal(uow),
        queue_job=QueueJob(uow, clock),
        process_jobs=process,
        get_context=GetContext(active),
        worker=JobWorker(process),
        require_service=service_dependency(ServiceTokenVerifier(settings.jwt_secret), "agent-orchestrator"),
        require_privacy=service_dependency(ServiceTokenVerifier(settings.jwt_secret), "privacy-service"),
        subject_store=SqlJiraSubjectStore(sessions, tables),
        closers=closers,
    )
