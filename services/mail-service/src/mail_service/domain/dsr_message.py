"""An email answering a data request (GDPR Art. 12). The row holds no personal data: the content is fetched
from privacy-service at the moment of sending. At most once, like every other outgoing message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .outgoing import GIVE_UP_AFTER, OutgoingStatus

_MAX_BACKOFF = 900.0


@dataclass(slots=True)
class DsrMessage:
    id: str  # dsr id : kind : attempt
    dsr_id: str
    kind: str
    attempt: int
    created_at: datetime
    next_try_at: datetime
    status: OutgoingStatus = OutgoingStatus.PENDING
    tries: int = 0
    detail: str | None = None
    trace: str | None = None  # W3C trace context of the data request

    def claim(self) -> None:
        self.status, self.tries = OutgoingStatus.SENDING, self.tries + 1

    def sent(self) -> None:
        self.status, self.detail = OutgoingStatus.SENT, None

    def failed(self, reason: str) -> None:
        self.status, self.detail = OutgoingStatus.FAILED, reason

    def try_later(self, now: datetime, reason: str) -> None:
        if now - self.created_at >= GIVE_UP_AFTER:
            self.failed(f"Gave up after 24 hours: {reason}")
            return
        wait = min(_MAX_BACKOFF, 60.0 * 2 ** max(0, self.tries - 1))
        self.status, self.detail = OutgoingStatus.PENDING, reason
        self.next_try_at = now + timedelta(seconds=wait)
