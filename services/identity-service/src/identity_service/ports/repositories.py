"""Persistence ports. Adapters: SQLAlchemy (production) and in-memory (tests)."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.user import User


class UserRepository(Protocol):
    async def get(self, user_id: str) -> User | None: ...
    async def get_by_email(self, email: str) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def save(self, user: User) -> None: ...
    async def list_all(self) -> list[User]: ...
    async def count_active_admins(self) -> int: ...


class EventRecorder(Protocol):
    """Collects domain events; the unit of work writes them to the outbox on commit."""

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    users: UserRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...
