"""An approved customer message on its way out: one row per recipient, tokenised until the moment of sending.

Sending is not idempotent, so a row is claimed (`sending`) before the provider is called. A row found in that
state after a crash may or may not have gone out: it is marked failed and never sent twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from opspilot_common.drafts import RECIPIENT_TOKEN

GIVE_UP_AFTER = timedelta(hours=24)
_MAX_BACKOFF = 900.0
_NO_NAME = re.compile(r"\s*" + re.escape(RECIPIENT_TOKEN))


class OutgoingStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(slots=True)
class OutgoingMessage:
    id: str
    message_id: str
    approval_id: str
    run_id: str
    thread_id: str
    email_id: str  # the email this answers: threading, and the proof of who may receive it
    email_token: str
    name_token: str | None
    subject: str
    body: str
    created_at: datetime
    next_try_at: datetime
    jira_key: str | None = None
    kind: str | None = None
    status: OutgoingStatus = OutgoingStatus.PENDING
    attempts: int = 0
    detail: str | None = None

    @property
    def display_token(self) -> str:
        return self.name_token or self.email_token

    def claim(self) -> None:
        self.status, self.attempts = OutgoingStatus.SENDING, self.attempts + 1

    def sent(self) -> None:
        self.status, self.detail = OutgoingStatus.SENT, None

    def failed(self, reason: str) -> None:
        self.status, self.detail = OutgoingStatus.FAILED, reason

    def try_later(self, now: datetime, reason: str, *, retry_after: float | None = None) -> None:
        """Back in the queue with a growing wait, until it has been failing for too long."""
        if now - self.created_at >= GIVE_UP_AFTER:
            self.failed(f"Gave up after 24 hours: {reason}")
            return
        wait = max(retry_after or 0.0, min(_MAX_BACKOFF, 60.0 * 2 ** max(0, self.attempts - 1)))
        self.status, self.detail = OutgoingStatus.PENDING, reason
        self.next_try_at = now + timedelta(seconds=wait)

    def personal_text(self) -> str:
        """The body with this recipient's name token in place of the placeholder (no name: no placeholder)."""
        if self.name_token:
            return self.body.replace(RECIPIENT_TOKEN, f"[{self.name_token}]")
        return _NO_NAME.sub("", self.body)
