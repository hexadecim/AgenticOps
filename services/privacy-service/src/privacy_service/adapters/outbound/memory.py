"""In-memory adapters for unit tests. The vault keeps plaintext; it never runs outside tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from opspilot_common.events.envelope import EventPayload

from ...domain.dsr import DataRequest
from ...domain.entities import EntityKind, normalise
from ...domain.records import PseudonymisationRecord, Reveal
from ...domain.tokens import make_token
from ...ports import VaultEntry


class InMemoryTokenVault:
    def __init__(self) -> None:
        self.entries: dict[str, VaultEntry] = {}
        self.counters: dict[EntityKind, int] = {}
        self.last_used: dict[str, datetime] = {}

    async def token_for(self, kind: EntityKind, value: str, *, subject: str | None) -> str:
        key = normalise(kind, value)
        for entry in self.entries.values():
            if entry.kind is kind and normalise(kind, entry.value) == key:
                self.last_used[entry.token] = datetime.now(UTC)
                return entry.token
        self.counters[kind] = self.counters.get(kind, 0) + 1
        token = make_token(kind, self.counters[kind])
        self.entries[token] = VaultEntry(token, kind, value, subject or token)
        self.last_used[token] = datetime.now(UTC)
        return token

    async def get(self, token: str) -> VaultEntry | None:
        return self.entries.get(token)

    async def same_subject(self, entry: VaultEntry) -> list[VaultEntry]:
        return [e for e in self.entries.values() if e.subject == entry.subject]

    async def find(self, kind: EntityKind, value: str) -> VaultEntry | None:
        key = normalise(kind, value)
        return next(
            (e for e in self.entries.values() if e.kind is kind and normalise(kind, e.value) == key), None
        )

    async def erase(self, tokens: list[str]) -> int:
        gone = [t for t in tokens if self.entries.pop(t, None) is not None]
        return len(gone)

    async def unused_since(self, cutoff: datetime, limit: int) -> list[str]:
        latest: dict[str, datetime] = {}
        for entry in self.entries.values():
            used = self.last_used[entry.token]
            subject = entry.subject or entry.token
            latest[subject] = max(latest.get(subject, used), used)
        return [s for s, used in latest.items() if used < cutoff][:limit]


class InMemoryRecordRepository:
    def __init__(self) -> None:
        self.rows: dict[str, PseudonymisationRecord] = {}

    async def by_email(self, email_id: str) -> PseudonymisationRecord | None:
        return self.rows.get(email_id)

    async def add(self, record: PseudonymisationRecord) -> None:
        self.rows[record.email_id] = record

    async def mentioning(self, tokens: list[str]) -> list[PseudonymisationRecord]:
        def named(r: PseudonymisationRecord) -> bool:
            e = r.email
            people = [a for a in (e.sender, *e.to, *e.cc)]
            bare = {t for a in people for t in (a.email_token, a.name_token) if t}
            return any(t in bare or f"[{t}]" in f"{e.subject}\n{e.body}" for t in tokens)

        return [r for r in self.rows.values() if named(r)]

    async def delete_emails(self, email_ids: list[str]) -> int:
        gone = [e for e in email_ids if self.rows.pop(e, None) is not None]
        return len(gone)


class InMemoryRevealRepository:
    def __init__(self) -> None:
        self.rows: list[Reveal] = []

    async def add(self, reveal: Reveal) -> None:
        self.rows.append(reveal)


class InMemoryRequestRepository:
    def __init__(self) -> None:
        self.rows: dict[str, DataRequest] = {}
        self.last = 0

    async def next_number(self) -> int:
        self.last += 1
        return self.last

    async def add(self, request: DataRequest) -> None:
        self.rows[request.id] = copy.deepcopy(request)

    async def save(self, request: DataRequest) -> None:
        self.rows[request.id] = copy.deepcopy(request)

    async def get(self, dsr_id: str) -> DataRequest | None:
        return copy.deepcopy(self.rows.get(dsr_id))

    async def get_for_update(self, dsr_id: str) -> DataRequest | None:
        return await self.get(dsr_id)

    async def all(self) -> list[DataRequest]:
        return [copy.deepcopy(r) for _, r in sorted(self.rows.items(), reverse=True)]


class InMemoryErasedEmails:
    def __init__(self) -> None:
        self.ids: set[str] = set()

    async def add(self, email_ids: list[str]) -> None:
        self.ids.update(email_ids)

    async def contains(self, email_id: str) -> bool:
        return email_id in self.ids


class InMemoryStore:
    def __init__(self) -> None:
        self.vault = InMemoryTokenVault()
        self.records = InMemoryRecordRepository()
        self.reveals = InMemoryRevealRepository()
        self.requests = InMemoryRequestRepository()
        self.erased = InMemoryErasedEmails()
        self.published: list[tuple[EventPayload, str | None]] = []


class InMemoryUnitOfWork:
    """Works on copies; commit writes them back, so a failure anywhere rolls everything back."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        self.vault = copy.deepcopy(self._store.vault)
        self.records = copy.deepcopy(self._store.records)
        self.reveals = copy.deepcopy(self._store.reveals)
        self.requests = copy.deepcopy(self._store.requests)
        self.erased = copy.deepcopy(self._store.erased)
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
        self._store.requests, self._store.erased = self.requests, self.erased
        self._store.published.extend(self._pending)
        self._pending = []
