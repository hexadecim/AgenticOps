"""Ports of agent-orchestrator. Graph nodes receive these through `config["configurable"]["ports"]`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.diagnosis import FixChoice
from ..domain.run import Run, RunStatus
from ..domain.triage import Intake


class Pseudonymiser(Protocol):
    async def pseudonymise(self, email_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(tokenised email, privacy record summary). Raise PrivacyUnavailableError or StopRunError."""
        ...


class IntakeModel(Protocol):
    async def classify(self, email: dict[str, Any]) -> Intake:
        """Raise LlmUnavailableError (park) or StopRunError (unusable answer)."""
        ...

    async def describe(self) -> dict[str, Any]:
        """Model name and options, shown to the approver ("deepseek-r1:8b · thinking on")."""
        ...


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Raise LlmUnavailableError."""
        ...


class IssueContext(Protocol):
    async def context(self, hours: int) -> dict[str, Any]:
        """{configured, project, issue_type, recent: [{key, summary, status, created}]}.
        Raise ContextUnavailableError."""
        ...


class EventSink(Protocol):
    async def emit(self, payload: EventPayload, *, actor: str | None = None) -> None:
        """Write the event to the outbox now (own transaction)."""
        ...


class ThreadTickets(Protocol):
    async def ticket_for_thread(self, thread_id: str, *, exclude_run: str) -> str | None:
        """Jira key of an earlier run on the same mail thread, if it got a ticket."""
        ...

    async def reporters(self, jira_key: str) -> int:
        """How many runs (reporters) are linked to the ticket."""
        ...

    async def remember_reporter(self, run_id: str, reporter: dict[str, Any]) -> None:
        """Keep the run's reporter (tokens and email id only), so a later resolution can reach them."""
        ...

    async def linked_reporters(self, jira_key: str, *, exclude_run: str) -> list[dict[str, Any] | None]:
        """Each other run linked to the ticket, oldest first; None for a run without a stored reporter."""
        ...


class OpsEvidence(Protocol):
    async def diagnose(
        self, *, text: str, site: str | None, asset: str | None, run_id: str, thread_id: str
    ) -> dict[str, Any]:
        """Probes, masked logs, runbooks and allow-listed candidates. Raise OpsUnavailableError."""
        ...

    async def probes(self, region: str | None, *, run_id: str, thread_id: str) -> list[dict[str, Any]]:
        """Fresh health probes. Raise OpsUnavailableError."""
        ...


class FixAdvisor(Protocol):
    async def choose(self, evidence: str, candidates: int) -> FixChoice:
        """Pick one numbered candidate or 'none'. Raise LlmUnavailableError or DiagnosisUnusableError."""
        ...


class CommsWriter(Protocol):
    async def write(self, brief: str) -> str:
        """The message text for this brief. Raise LlmUnavailableError or DraftUnusableError."""
        ...


@dataclass(frozen=True)
class AgentPorts:
    privacy: Pseudonymiser
    intake: IntakeModel
    embedder: Embedder
    issues: IssueContext
    events: EventSink
    threads: ThreadTickets
    ops: OpsEvidence | None = None  # None: diagnostics is skipped (the run ends at the ticket)
    advisor: FixAdvisor | None = None
    comms: CommsWriter | None = None  # None: no customer messages (the run ends at its outcome)


class LlmCircuit(Protocol):
    async def retry_after(self) -> float | None:
        """Seconds until the model's circuit allows calls again; None when it does."""
        ...


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...
    async def get_for_update(self, run_id: str) -> Run | None:
        """Held until the transaction ends: the worker and a result arriving at the same moment take turns."""
        ...

    async def by_email(self, email_id: str) -> Run | None: ...
    async def by_approval(self, approval_id: str) -> Run | None: ...
    async def add(self, run: Run) -> None: ...
    async def save(self, run: Run) -> None: ...
    async def due(self, now: datetime, limit: int) -> list[Run]: ...
    async def requeue_interrupted(self, now: datetime) -> int:
        """Runs left `running` by a crash go back to the queue."""
        ...

    async def counts(self, since: datetime) -> dict[RunStatus, int]: ...
    async def ticket_for_thread(self, thread_id: str, *, exclude_run: str) -> str | None: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    runs: RunRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
