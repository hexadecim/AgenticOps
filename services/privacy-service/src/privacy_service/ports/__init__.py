"""Ports of privacy-service. Detection, the raw-email source and the vault are separate (ISP)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from opspilot_common.events.envelope import EventPayload
from opspilot_common.subjects import StoreReport, SubjectQuery

from ..domain.dsr import DataRequest
from ..domain.entities import EntityKind, Identity, Span
from ..domain.records import PseudonymisationRecord, Reveal


class EntityDetector(Protocol):
    async def detect(self, text: str, language: str) -> list[Span]:
        """Personal data in `text`. Raise PrivacyUnavailableError when detection cannot be trusted."""
        ...


@dataclass(frozen=True, slots=True)
class RawEmail:
    email_id: str
    thread_id: str
    sender: Identity
    to: tuple[Identity, ...]
    cc: tuple[Identity, ...]
    subject: str
    body: str
    received_at: datetime
    attachments: tuple[dict[str, Any], ...] = field(default=())


class RawEmailSource(Protocol):
    async def get(self, email_id: str) -> RawEmail:
        """Raise EmailNotFoundError, or PrivacyUnavailableError when mail-service cannot be asked."""
        ...


@dataclass(frozen=True, slots=True)
class VaultEntry:
    token: str
    kind: EntityKind
    value: str  # plaintext, in memory only
    subject: str | None


class TokenVault(Protocol):
    """Token ↔ value store. Values are encrypted at rest and looked up by keyed hash, never in plaintext."""

    async def token_for(self, kind: EntityKind, value: str, *, subject: str | None) -> str:
        """The existing token for this value, or a new one."""
        ...

    async def get(self, token: str) -> VaultEntry | None: ...
    async def same_subject(self, entry: VaultEntry) -> list[VaultEntry]:
        """All values belonging to the same person as `entry` (including itself)."""
        ...

    async def find(self, kind: EntityKind, value: str) -> VaultEntry | None:
        """By keyed hash: the value itself is never compared in plaintext."""
        ...

    async def erase(self, tokens: list[str]) -> int: ...
    async def unused_since(self, cutoff: datetime, limit: int) -> list[str]:
        """One token per person none of whose tokens has been used since `cutoff`."""
        ...


class RecordRepository(Protocol):
    async def by_email(self, email_id: str) -> PseudonymisationRecord | None: ...
    async def add(self, record: PseudonymisationRecord) -> None: ...
    async def mentioning(self, tokens: list[str]) -> list[PseudonymisationRecord]: ...
    async def delete_emails(self, email_ids: list[str]) -> int: ...


class RevealRepository(Protocol):
    async def add(self, reveal: Reveal) -> None: ...


class RequestRepository(Protocol):
    async def next_number(self) -> int: ...
    async def add(self, request: DataRequest) -> None: ...
    async def save(self, request: DataRequest) -> None: ...
    async def get(self, dsr_id: str) -> DataRequest | None: ...
    async def all(self) -> list[DataRequest]: ...


class ErasedEmails(Protocol):
    async def add(self, email_ids: list[str]) -> None: ...
    async def contains(self, email_id: str) -> bool: ...


class SubjectStores(Protocol):
    async def gather(self, query: SubjectQuery) -> list[StoreReport]:
        """Every other service's report. A store that cannot answer comes back with `available=False`."""
        ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    vault: TokenVault
    records: RecordRepository
    reveals: RevealRepository
    requests: RequestRepository
    erased: ErasedEmails
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
