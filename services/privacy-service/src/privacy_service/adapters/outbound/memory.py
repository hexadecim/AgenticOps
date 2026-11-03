"""In-memory adapters for unit tests. The vault keeps plaintext; it never runs outside tests."""

from __future__ import annotations

import copy
from types import TracebackType
from typing import Self

from opspilot_common.events.envelope import EventPayload

from ...domain.entities import EntityKind, normalise
from ...domain.records import PseudonymisationRecord, Reveal
from ...domain.tokens import make_token
from ...ports import VaultEntry


class InMemoryTokenVault:
    def __init__(self) -> None:
        self.entries: dict[str, VaultEntry] = {}
        self.counters: dict[EntityKind, int] = {}

    async def token_for(self, kind: EntityKind, value: str, *, subject: str | None) -> str:
        key = normalise(kind, value)
        for entry in self.entries.values():
            if entry.kind is kind and normalise(kind, entry.value) == key:
                return entry.token
        self.counters[kind] = self.counters.get(kind, 0) + 1
        token = make_token(kind, self.counters[kind])
        self.entries[token] = VaultEntry(token, kind, value, subject or token)
        return token

    async def get(self, token: str) -> VaultEntry | None:
        return self.entries.get(token)

    async def same_subject(self, entry: VaultEntry) -> list[VaultEntry]:
        return [e for e in self.entries.values() if e.subject == entry.subject]


class InMemoryRecordRepository:
    def __init__(self) -> None:
        self.rows: dict[str, PseudonymisationRecord] = {}

    async def by_email(self, email_id: str) -> PseudonymisationRecord | None:
        return self.rows.get(email_id)

    async def add(self, record: PseudonymisationRecord) -> None:
        self.rows[record.email_id] = record


class InMemoryRevealRepository:
    def __init__(self) -> None:
        self.rows: list[Reveal] = []

    async def add(self, reveal: Reveal) -> None:
        self.rows.append(reveal)


class InMemoryStore:
    def __init__(self) -> None:
        self.vault = InMemoryTokenVault()
        self.records = InMemoryRecordRepository()
        self.reveals = InMemoryRevealRepository()
        self.published: list[tuple[EventPayload, str | None]] = []


class InMemoryUnitOfWork:
    """Works on copies; commit writes them back, so a failure anywhere rolls everything back."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        self.vault = copy.deepcopy(self._store.vault)
        self.records = copy.deepcopy(self._store.records)
        self.reveals = copy.deepcopy(self._store.reveals)
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
        self._store.vault, self._store.records, self._store.reveals = self.vault, self.records, self.reveals
        self._store.published.extend(self._pending)
        self._pending = []
