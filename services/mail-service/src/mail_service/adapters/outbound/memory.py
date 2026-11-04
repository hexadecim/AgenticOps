"""In-memory adapters for unit tests."""

from __future__ import annotations

import copy
from datetime import datetime
from types import TracebackType
from typing import Self

from opspilot_common.events.envelope import EventPayload

from ...domain.dsr_message import DsrMessage
from ...domain.mail import StoredEmail
from ...domain.outgoing import OutgoingMessage, OutgoingStatus
from ...domain.status import PollState


class InMemoryEmailRepository:
    def __init__(self) -> None:
        self.rows: dict[str, StoredEmail] = {}

    async def exists(self, dedupe_key: str) -> bool:
        return any(e.mail.dedupe_key(e.provider) == dedupe_key for e in self.rows.values())

    async def add(self, email: StoredEmail) -> None:
        self.rows[email.id] = email

    async def get(self, email_id: str) -> StoredEmail | None:
        return self.rows.get(email_id)

    async def thread_of_message(self, message_ids: list[str]) -> str | None:
        by_id = {e.mail.message_id: e.thread_id for e in self.rows.values() if e.mail.message_id}
        return next((by_id[i] for i in message_ids if i in by_id), None)

    async def thread_of_provider_thread(self, provider: str, provider_thread_id: str) -> str | None:
        return next(
            (
                e.thread_id
                for e in self.rows.values()
                if e.provider == provider and e.mail.provider_thread_id == provider_thread_id
            ),
            None,
        )

    async def delete_fetched_before(self, cutoff: datetime) -> int:
        old = [k for k, e in self.rows.items() if e.fetched_at < cutoff]
        for key in old:
            del self.rows[key]
        return len(old)


class InMemoryPollStateRepository:
    def __init__(self) -> None:
        self.state = PollState()

    async def get(self) -> PollState:
        return copy.deepcopy(self.state)

    async def save(self, state: PollState) -> None:
        self.state = copy.deepcopy(state)


class InMemoryOutgoingRepository:
    def __init__(self) -> None:
        self.rows: dict[str, OutgoingMessage] = {}
        self.approved: dict[str, datetime] = {}

    async def exists(self, message_id: str) -> bool:
        return any(r.message_id == message_id for r in self.rows.values())

    async def add(self, message: OutgoingMessage) -> None:
        self.rows[message.id] = copy.copy(message)

    async def save(self, message: OutgoingMessage) -> None:
        self.rows[message.id] = copy.copy(message)

    async def approve(self, approval_id: str, at: datetime) -> None:
        self.approved.setdefault(approval_id, at)

    async def due(self, now: datetime, limit: int) -> list[OutgoingMessage]:
        due = [
            r
            for r in self.rows.values()
            if r.status is OutgoingStatus.PENDING and r.next_try_at <= now and r.approval_id in self.approved
        ]
        return [copy.copy(r) for r in sorted(due, key=lambda r: r.created_at)[:limit]]

    async def of_message(self, message_id: str) -> list[OutgoingMessage]:
        return [copy.copy(r) for r in self.rows.values() if r.message_id == message_id]

    async def interrupted(self) -> list[OutgoingMessage]:
        return [copy.copy(r) for r in self.rows.values() if r.status is OutgoingStatus.SENDING]

    async def delete_created_before(self, cutoff: datetime) -> int:
        done = (OutgoingStatus.SENT, OutgoingStatus.FAILED)
        old = [k for k, r in self.rows.items() if r.created_at < cutoff and r.status in done]
        for key in old:
            del self.rows[key]
        return len(old)


class InMemoryDsrMessageRepository:
    def __init__(self) -> None:
        self.rows: dict[str, DsrMessage] = {}

    async def exists(self, message_id: str) -> bool:
        return message_id in self.rows

    async def add(self, message: DsrMessage) -> None:
        self.rows[message.id] = copy.copy(message)

    async def save(self, message: DsrMessage) -> None:
        self.rows[message.id] = copy.copy(message)

    async def due(self, now: datetime, limit: int) -> list[DsrMessage]:
        due = [r for r in self.rows.values() if r.status is OutgoingStatus.PENDING and r.next_try_at <= now]
        return [copy.copy(r) for r in due[:limit]]

    async def interrupted(self) -> list[DsrMessage]:
        return [copy.copy(r) for r in self.rows.values() if r.status is OutgoingStatus.SENDING]


class InMemoryStore:
    def __init__(self) -> None:
        self.emails = InMemoryEmailRepository()
        self.poll_state = InMemoryPollStateRepository()
        self.outgoing = InMemoryOutgoingRepository()
        self.dsr_messages = InMemoryDsrMessageRepository()
        self.published: list[tuple[EventPayload, str | None]] = []


class InMemoryUnitOfWork:
    """Copies the store on enter and writes it back on commit, so a rollback discards changes."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        self.emails = copy.deepcopy(self._store.emails)
        self.poll_state = copy.deepcopy(self._store.poll_state)
        self.outgoing = copy.deepcopy(self._store.outgoing)
        self.dsr_messages = copy.deepcopy(self._store.dsr_messages)
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
        self._store.dsr_messages = copy.deepcopy(self.dsr_messages)
        self._store.emails, self._store.poll_state, self._store.outgoing = (
            copy.deepcopy(self.emails),
            copy.deepcopy(self.poll_state),
            copy.deepcopy(self.outgoing),
        )
        self._store.published.extend(self._pending)
        self._pending.clear()
