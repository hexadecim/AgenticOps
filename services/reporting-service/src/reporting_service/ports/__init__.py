"""Ports of reporting-service: read-model stores. Reporting publishes nothing and calls no other service."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from ..domain.model import Case, Dependency, TimelineEntry


class CaseStore(Protocol):
    async def get(self, run_id: str) -> Case | None: ...
    async def save(self, case: Case) -> None: ...
    async def by_email(self, email_id: str) -> list[Case]: ...
    async def by_ticket(self, jira_key: str) -> list[Case]: ...
    async def active_or_since(self, since: datetime) -> list[Case]:
        """Cases still open, plus any that changed since `since`."""
        ...

    async def incidents(self, limit: int) -> list[Case]:
        """Cases with a Jira key, newest first."""
        ...


class EmailStore(Protocol):
    async def received(self, email_id: str) -> datetime | None: ...
    async def record(self, email_id: str, at: datetime) -> None: ...


class TimelineStore(Protocol):
    async def add(self, entry: TimelineEntry) -> None: ...
    async def for_cases(
        self, run_ids: list[str], thread_ids: list[str], *, start: datetime, end: datetime
    ) -> list[TimelineEntry]:
        """Entries of these runs/threads, plus system entries between `start` and `end`, oldest first."""
        ...


class DependencyStore(Protocol):
    async def get(self, name: str) -> Dependency | None: ...
    async def save(self, dependency: Dependency) -> None: ...
    async def all(self) -> list[Dependency]: ...


class UnitOfWork(Protocol):
    cases: CaseStore
    emails: EmailStore
    timeline: TimelineStore
    dependencies: DependencyStore

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
