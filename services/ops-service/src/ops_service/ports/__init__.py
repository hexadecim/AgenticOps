"""Ports of ops-service. Narrow on purpose: probing, reading logs, retrieving runbooks and running actions are
separate capabilities with separate adapters (mock environment, HTTP probes, log files, pgvector, ...)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.model import (
    Environment,
    FixJob,
    FixProposal,
    LogFinding,
    OpsSettings,
    ProbeResult,
    Runbook,
    RunbookChunk,
)


class EnvironmentSource(Protocol):
    async def environment(self) -> Environment: ...


class ProbeRunner(Protocol):
    async def run(self, region: str | None) -> list[ProbeResult]:
        """Raise EvidenceUnavailableError when probing itself is impossible."""
        ...


class LogSource(Protocol):
    async def search(self, folder: str, services: list[str], *, limit: int = 3) -> list[LogFinding]:
        """Most recent ERROR/WARN lines per service, already masked."""
        ...


class RunbookSource(Protocol):
    async def load(self, folder: str) -> list[Runbook]: ...


class Embedder(Protocol):
    async def embed(self, settings: OpsSettings, texts: list[str]) -> list[list[float]]:
        """Raise EmbeddingUnavailableError."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    detail: str


class ActionExecutor(Protocol):
    name: str

    async def execute(self, action: str, params: dict[str, str], *, key: str) -> ExecutionResult:
        """Run an allow-listed action once per key (a repeat with the same key does not run it again).
        Raise ExecutorUnavailableError to be retried later."""
        ...


class SettingsSource(Protocol):
    async def ops(self) -> OpsSettings:
        """The admin's Ops settings merged with the embedding model; defaults when never saved.
        Raise SettingsUnavailableError."""
        ...


class RunbookIndex(Protocol):
    async def fingerprint(self) -> str | None: ...
    async def replace(self, fingerprint: str, chunks: list[tuple[RunbookChunk, list[float]]]) -> None: ...
    async def nearest(self, vector: list[float], limit: int) -> list[tuple[str, int, float]]:
        """(runbook id, chunk index, cosine similarity), best first."""
        ...


class ProposalRepository(Protocol):
    async def get(self, approval_id: str) -> FixProposal | None: ...
    async def add(self, proposal: FixProposal) -> None: ...


class JobRepository(Protocol):
    async def get(self, job_id: str) -> FixJob | None: ...
    async def add(self, job: FixJob) -> None: ...
    async def save(self, job: FixJob) -> None: ...
    async def due(self, now: datetime, limit: int) -> list[FixJob]: ...


class ProbeStateRepository(Protocol):
    async def last(self, probe: str, region: str | None) -> bool | None: ...
    async def set(self, probe: str, region: str | None, ok: bool, now: datetime) -> None: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    proposals: ProposalRepository
    jobs: JobRepository
    probe_states: ProbeStateRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
