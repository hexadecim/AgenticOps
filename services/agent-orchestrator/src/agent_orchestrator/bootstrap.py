"""Composition root for agent-orchestrator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opspilot_common.auth import Principal, ServiceTokenIssuer, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.inbound.worker import RunWorker
from .adapters.outbound.llm import (
    LlmSettingsCache,
    OllamaCircuit,
    OllamaCommsWriter,
    OllamaEmbedder,
    OllamaFixAdvisor,
    OllamaIntake,
)
from .adapters.outbound.services_http import JiraContextClient, OpsClient, PrivacyClient
from .adapters.outbound.sql import OrchestratorTables, SqlEventSink, SqlThreadTickets, SqlUnitOfWork
from .application import (
    AdvanceRuns,
    RecordDecision,
    RecordFixResult,
    RecordMessageResult,
    RecordTicketResult,
    RunSummary,
    StartRun,
    build_graph,
)
from .config import Settings
from .ports import AgentPorts, Clock, LlmCircuit, UnitOfWork

SERVICE = "agent-orchestrator"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: OrchestratorTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    clock: Clock
    ports: AgentPorts
    llm_settings: LlmSettingsCache
    llm_circuit: LlmCircuit
    start_run: StartRun
    record_decision: RecordDecision
    record_ticket: RecordTicketResult
    record_fix: RecordFixResult
    record_message: RecordMessageResult
    run_summary: RunSummary
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]

    def uow(self) -> UnitOfWork:
        return SqlUnitOfWork(self.sessions, self.tables)

    def runner(self, checkpointer: Any) -> tuple[AdvanceRuns, RunWorker]:
        """The graph needs an open checkpointer, so the worker is assembled in the app's lifespan."""
        graph = build_graph().compile(checkpointer=checkpointer)
        advance = AdvanceRuns(self.uow, graph, self.ports, self.llm_circuit, self.clock)
        return advance, RunWorker(advance)


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    ports: AgentPorts | None = None,
    llm_circuit: LlmCircuit | None = None,
    clock: Clock | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = OrchestratorTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    tokens = ServiceTokenIssuer(settings.jwt_secret, SERVICE)

    def http(name: str, url: str) -> ResilientHttpClient:
        client = ResilientHttpClient.from_registry(registry, name, base_url=url)
        closers.append(client.aclose)
        return client

    llm_settings = LlmSettingsCache(http("settings-service", settings.settings_url), tokens)
    if ports is None:
        ports = AgentPorts(
            privacy=PrivacyClient(http("privacy-service", settings.privacy_url), tokens),
            intake=OllamaIntake(llm_settings, registry.get("ollama")),
            embedder=OllamaEmbedder(llm_settings, registry.get("ollama-embed")),
            issues=JiraContextClient(http("jira-connector", settings.jira_connector_url), tokens),
            events=SqlEventSink(sessions, tables),
            threads=SqlThreadTickets(sessions, tables),
            ops=OpsClient(http("ops-service", settings.ops_url), tokens),
            advisor=OllamaFixAdvisor(llm_settings, registry.get("ollama")),
            comms=OllamaCommsWriter(llm_settings, registry.get("ollama")),
        )
    clock = clock or SystemClock()
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        clock=clock,
        ports=ports,
        llm_settings=llm_settings,
        llm_circuit=llm_circuit or OllamaCircuit(registry.get("ollama")),
        start_run=StartRun(uow, clock),
        record_decision=RecordDecision(uow, clock),
        record_ticket=RecordTicketResult(uow, clock),
        record_fix=RecordFixResult(uow, clock),
        record_message=RecordMessageResult(uow, clock),
        run_summary=RunSummary(uow, clock),
        current_user=principal_dependency(TokenVerifier(settings.jwt_secret)),
        closers=closers,
    )
