"""In-memory mailbox for tests. It must pass the same contract suite as the real providers (Liskov)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ....domain.errors import MailError
from ....domain.mail import FetchBatch, OutgoingMail
from .mime import build_mime, parse_mime


class FakeMailbox:
    can_send = True

    def __init__(self, address: str = "support@example.com") -> None:
        self.address = address
        self.messages: list[tuple[int, bytes, datetime | None]] = []
        self.sent: list[bytes] = []
        self.fail_with: MailError | None = None
        self._next_uid = 1

    def deliver(self, raw: bytes, received_at: datetime | None = None) -> None:
        self.messages.append((self._next_uid, raw, received_at))
        self._next_uid += 1

    async def fetch_new(self, cursor: Mapping[str, Any] | None, *, limit: int, since: datetime) -> FetchBatch:
        if self.fail_with:
            raise self.fail_with
        parsed = [
            (uid, parse_mime(raw, provider_id=str(uid), received_at=at)) for uid, raw, at in self.messages
        ]
        if cursor:
            pending = [(u, m) for u, m in parsed if u > int(cursor["last"])]
        else:
            pending = [(u, m) for u, m in parsed if m.received_at >= since]
        batch = pending[:limit]
        last = batch[-1][0] if batch else (int(cursor["last"]) if cursor else self._next_uid - 1)
        return FetchBatch([m for _, m in batch], {"last": last}, len(pending) > limit)

    async def send(self, mail: OutgoingMail) -> str:
        if self.fail_with:
            raise self.fail_with
        raw, message_id = build_mime(mail, self.address)
        self.sent.append(raw)
        return message_id
