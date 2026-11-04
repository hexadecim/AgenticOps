"""Ports of approval-service."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from opspilot_common.events.catalogue import Gate
from opspilot_common.events.envelope import EventPayload

from ..domain.approval import Approval, Status, WaitingStats


class ApprovalRepository(Protocol):
    async def get(self, approval_id: str) -> Approval | None: ...
    async def get_for_decision(self, approval_id: str) -> Approval | None:
        """Holds the approval until the transaction ends: two people deciding at once are served in turn."""
        ...

    async def add(self, approval: Approval) -> None: ...
    async def save(self, approval: Approval) -> None: ...
    async def decided(self, *, gate: Gate | None = None, limit: int = 200) -> list[Approval]:
        """Latest decision first."""
        ...

    async def list(
        self, *, statuses: set[Status], gate: Gate | None = None, limit: int = 200
    ) -> list[Approval]:
        """Oldest first."""
        ...

    async def waiting(self) -> WaitingStats:
        """Over everything waiting, however long the queue is."""
        ...

    async def latest_for_run(self, run_id: str, gate: Gate) -> Approval | None: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    approvals: ApprovalRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
