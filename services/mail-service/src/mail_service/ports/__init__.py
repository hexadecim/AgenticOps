"""Ports of mail-service. Mailbox ports are deliberately narrow: reading and sending are separate (ISP)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.dsr_message import DsrMessage
from ..domain.mail import FetchBatch, MailboxConfig, OutgoingMail, StoredEmail
from ..domain.outgoing import OutgoingMessage
from ..domain.status import PollState


class MailReader(Protocol):
    async def fetch_new(self, cursor: Mapping[str, Any] | None, *, limit: int, since: datetime) -> FetchBatch:
        """Messages that arrived after `cursor`, oldest first, at most `limit`.

        Without a cursor (first poll, or another mailbox), start at messages received since `since`.
        Never modifies the mailbox. Raises MailboxUnavailableError or MailboxRejectedError.
        """
        ...


class MailSender(Protocol):
    async def send(self, mail: OutgoingMail) -> str:
        """Send from the configured mailbox; return the Message-ID of the sent message."""
        ...


@dataclass
class MailboxConnection:
    """A configured provider. `sender` is None when the configuration cannot send (e.g. no SMTP server)."""

    provider: str
    reader: MailReader
    sender: MailSender | None
    close: Callable[[], Awaitable[None]]


class MailboxBuilder(Protocol):
    """Builds the adapter for a configuration. Implemented by the provider registry (open/closed)."""

    def build(self, config: MailboxConfig) -> MailboxConnection: ...


class SettingsSource(Protocol):
    async def mailbox(self) -> MailboxConfig | None:
        """The current mailbox settings, or None when none are saved. Raises SettingsUnavailableError."""
        ...


class CircuitProbe(Protocol):
    async def state(self) -> tuple[str, float | None]:
        """The mail-provider circuit: (closed | open | half_open, seconds until the next try when open)."""
        ...


class EmailRepository(Protocol):
    async def exists(self, dedupe_key: str) -> bool: ...
    async def add(self, email: StoredEmail) -> None: ...
    async def get(self, email_id: str) -> StoredEmail | None: ...
    async def thread_of_message(self, message_ids: list[str]) -> str | None:
        """Thread id of any stored message whose Message-ID is in `message_ids`."""
        ...

    async def thread_of_provider_thread(self, provider: str, provider_thread_id: str) -> str | None: ...
    async def delete_fetched_before(self, cutoff: datetime) -> int: ...


class Detokeniser(Protocol):
    async def detokenise(self, text: str, *, thread_id: str) -> str:
        """Real values in place of tokens. Raise DetokeniseUnavailableError or DetokeniseRefusedError."""
        ...


class OutgoingRepository(Protocol):
    async def exists(self, message_id: str) -> bool: ...
    async def add(self, message: OutgoingMessage) -> None: ...
    async def save(self, message: OutgoingMessage) -> None: ...
    async def claim(self, message: OutgoingMessage) -> bool:
        """Mark it as being sent. False when another sender got there first: then it must not be sent here."""
        ...

    async def approve(self, approval_id: str, at: datetime) -> None:
        """A person approved this message (`approval.decided`). Safe to repeat."""
        ...

    async def due(self, now: datetime, limit: int) -> list[OutgoingMessage]:
        """Pending rows that are due and whose approval has been seen; oldest first."""
        ...

    async def of_message(self, message_id: str) -> list[OutgoingMessage]: ...
    async def interrupted(self) -> list[OutgoingMessage]:
        """Rows a crash left in `sending`."""
        ...

    async def delete_created_before(self, cutoff: datetime) -> int: ...


class DsrMessageRepository(Protocol):
    async def exists(self, message_id: str) -> bool: ...
    async def add(self, message: DsrMessage) -> None: ...
    async def save(self, message: DsrMessage) -> None: ...
    async def claim(self, message: DsrMessage) -> bool:
        """See OutgoingRepository.claim."""
        ...

    async def due(self, now: datetime, limit: int) -> list[DsrMessage]: ...
    async def interrupted(self) -> list[DsrMessage]: ...


class DsrContentSource(Protocol):
    async def outbound(self, dsr_id: str, kind: str) -> OutgoingMail:
        """Raise DetokeniseUnavailableError to try later, DetokeniseRefusedError when it must not go out."""
        ...


class PollStateRepository(Protocol):
    async def get(self) -> PollState: ...
    async def save(self, state: PollState) -> None: ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    dsr_messages: DsrMessageRepository
    emails: EmailRepository
    poll_state: PollStateRepository
    outgoing: OutgoingRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
