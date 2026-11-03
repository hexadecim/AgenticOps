"""Ports of jira-connector. Issue operations are separate narrow ports (ISP); one Jira adapter implements
all of them."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.model import IssueSummary, JiraSettings, Job, Proposal, TicketFields


class IssueCreator(Protocol):
    async def create(self, fields: TicketFields, labels: list[str]) -> str:
        """Return the new issue key. Raise TrackerUnavailableError or TrackerRejectedError."""
        ...


class IssueFinder(Protocol):
    async def find_by_label(self, label: str) -> str | None: ...


class IssueSearcher(Protocol):
    async def recent(self, hours: int) -> list[IssueSummary]:
        """Open issues of the project created in the last `hours`, newest first."""
        ...


class IssueCommenter(Protocol):
    async def comment(self, key: str, text: str) -> None: ...


class JiraTracker(IssueCreator, IssueFinder, IssueSearcher, IssueCommenter, Protocol):
    """What one configured Jira site offers; use cases depend on the narrow ports above."""

    async def aclose(self) -> None: ...


class TrackerFactory(Protocol):
    def __call__(self, settings: JiraSettings) -> JiraTracker: ...


class SettingsSource(Protocol):
    async def jira(self) -> JiraSettings | None:
        """None when Jira is not configured. Raise SettingsUnavailableError."""
        ...


class ProposalRepository(Protocol):
    async def get(self, approval_id: str) -> Proposal | None: ...
    async def add(self, proposal: Proposal) -> None: ...


class JobRepository(Protocol):
    async def get(self, job_id: str) -> Job | None: ...
    async def add(self, job: Job) -> None: ...
    async def save(self, job: Job) -> None: ...
    async def due(self, now: datetime, limit: int) -> list[Job]:
        """Pending jobs whose next attempt is due, oldest first."""
        ...

    async def pending_count(self) -> int: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    proposals: ProposalRepository
    jobs: JobRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
