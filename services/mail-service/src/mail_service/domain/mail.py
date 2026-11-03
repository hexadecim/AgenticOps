"""Mail as OpsPilot sees it, independent of the provider it came from."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def normalise_message_id(value: str | None) -> str | None:
    """`<ABC@host>` → `abc@host`. Message-IDs are compared case-insensitively and without brackets."""
    if not value:
        return None
    cleaned = value.strip().strip("<>").strip().lower()
    return cleaned or None


@dataclass(frozen=True, slots=True)
class Address:
    email: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class Attachment:
    """Metadata only: attachment content is never stored or passed on."""

    filename: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class IncomingMail:
    """One message as read from a mailbox, already parsed."""

    provider_id: str  # the provider's own id (IMAP UID, Gmail message id, Graph message id)
    message_id: str | None  # RFC 5322 Message-ID, normalised
    subject: str
    sender: Address
    received_at: datetime
    body_text: str
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    provider_thread_id: str | None = None  # Gmail threadId, Graph conversationId
    body_html: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def dedupe_key(self, provider: str) -> str:
        """The same message seen twice (re-poll, overlap, second replica) has the same key."""
        return f"mid:{self.message_id}" if self.message_id else f"{provider}:{self.provider_id}"

    def parent_ids(self) -> list[str]:
        """Message-IDs this message replies to, closest parent first."""
        ids = [self.in_reply_to] if self.in_reply_to else []
        ids += [r for r in reversed(self.references) if r not in ids]
        return ids


@dataclass(frozen=True, slots=True)
class StoredEmail:
    id: str
    thread_id: str
    provider: str
    fetched_at: datetime
    mail: IncomingMail


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    to: tuple[Address, ...]
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchBatch:
    """New messages, oldest first, and where to continue next time. `more` = the limit cut the batch."""

    mails: list[IncomingMail]
    cursor: dict[str, Any]
    more: bool = False


# Values that identify *which* mailbox is configured; a change means the old read position is meaningless.
_IDENTITY_KEYS = ("imap_host", "imap_port", "username", "gmail_user", "graph_tenant_id", "graph_mailbox")


@dataclass(frozen=True, slots=True)
class MailboxConfig:
    """The resolved mailbox settings (from settings-service). Secrets are plaintext only in memory."""

    version: int
    provider: str
    folder: str
    poll_interval_seconds: int
    values: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def fingerprint(self) -> str:
        parts = [self.provider, self.folder, *(str(self.values.get(k, "")) for k in _IDENTITY_KEYS)]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]

    @property
    def address(self) -> str:
        """The mailbox's own address, used as sender for replies."""
        return str(
            self.values.get("username") or self.values.get("gmail_user") or self.values.get("graph_mailbox")
        )
