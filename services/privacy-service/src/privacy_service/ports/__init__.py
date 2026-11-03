"""Ports of privacy-service. Detection, the raw-email source and the vault are separate (ISP)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from opspilot_common.events.envelope import EventPayload

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


class RecordRepository(Protocol):
    async def by_email(self, email_id: str) -> PseudonymisationRecord | None: ...
    async def add(self, record: PseudonymisationRecord) -> None: ...


class RevealRepository(Protocol):
    async def add(self, reveal: Reveal) -> None: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    vault: TokenVault
    records: RecordRepository
    reveals: RevealRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
