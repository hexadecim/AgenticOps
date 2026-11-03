"""In-memory adapters for unit tests and contract tests."""

from __future__ import annotations

import copy
from types import TracebackType
from typing import Self

from opspilot_common.auth import Role
from opspilot_common.events.envelope import EventPayload

from ...domain.user import User


class InMemoryUserRepository:
    def __init__(self) -> None:
        self.rows: dict[str, User] = {}

    async def get(self, user_id: str) -> User | None:
        return copy.deepcopy(self.rows.get(user_id))

    async def get_by_email(self, email: str) -> User | None:
        return copy.deepcopy(next((u for u in self.rows.values() if u.email == email), None))

    async def add(self, user: User) -> None:
        if any(u.email == user.email for u in self.rows.values()):
            raise ValueError("duplicate email")
        self.rows[user.id] = copy.deepcopy(user)

    async def save(self, user: User) -> None:
        self.rows[user.id] = copy.deepcopy(user)

    async def list_all(self) -> list[User]:
        return [copy.deepcopy(u) for u in self.rows.values()]

    async def count_active_admins(self) -> int:
        return sum(1 for u in self.rows.values() if u.active and Role.ADMIN in u.roles)


class InMemoryUnitOfWork:
    """Stages writes on a copy and applies them on commit, mimicking a transaction."""

    def __init__(
        self, store: InMemoryUserRepository, published: list[tuple[EventPayload, str | None]]
    ) -> None:
        self._store, self._published = store, published

    async def __aenter__(self) -> Self:
        self.users = InMemoryUserRepository()
        self.users.rows = copy.deepcopy(self._store.rows)
        self._pending: list[tuple[EventPayload, str | None]] = []
        self.events = self
        return self

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self._pending.append((payload, actor))

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._pending.clear()

    async def commit(self) -> None:
        self._store.rows = copy.deepcopy(self.users.rows)
        self._published.extend(self._pending)
        self._pending.clear()
