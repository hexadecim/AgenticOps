"""Ports of approval-service."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from opspilot_common.events.catalogue import Gate
from opspilot_common.events.envelope import EventPayload

from ..domain.approval import Approval, Status


class ApprovalRepository(Protocol):
    async def get(self, approval_id: str) -> Approval | None: ...
    async def add(self, approval: Approval) -> None: ...
    async def save(self, approval: Approval) -> None: ...
    async def list(
        self, *, statuses: set[Status], gate: Gate | None = None, limit: int = 200
    ) -> list[Approval]:
        """Oldest first."""
        ...


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
