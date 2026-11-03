from __future__ import annotations

from datetime import UTC, datetime

from privacy_service.domain.entities import EntityKind, Identity, Source, Span
from privacy_service.domain.errors import EmailNotFoundError, PrivacyUnavailableError
from privacy_service.ports import RawEmail

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

BODY = (
    "Hello support,\n\n"
    "Since about 08:40 our upload is stuck at 0%. My colleague Maria Schmidt sees the same.\n"
    "Alex here again: please call me on +49 40 1234567.\n\n"
    "Regards,\nAlex Muster\nACME GmbH, Hafenstraße 1, Hamburg\n"
)


def raw_email(email_id: str = "e-1", thread_id: str = "thr-0001-aaaa", **changes: object) -> RawEmail:
    values: dict[str, object] = {
        "email_id": email_id,
        "thread_id": thread_id,
        "sender": Identity("alex.muster@example.eu", "Alex Muster"),
        "to": (Identity("support@opspilot.local", "Support"),),
        "cc": (),
        "subject": "Upload stuck at 0%, Alex Muster",
        "body": BODY,
        "received_at": NOW,
        "attachments": ({"filename": "Alex_Muster_screenshot.png", "content_type": "image/png", "size": 10},),
    }
    values.update(changes)
    return RawEmail(**values)  # type: ignore[arg-type]


class FakeSource:
    def __init__(self, *emails: RawEmail) -> None:
        self.emails = {e.email_id: e for e in emails}
        self.down = False

    async def get(self, email_id: str) -> RawEmail:
        if self.down:
            raise PrivacyUnavailableError("mail-service is unavailable")
        if email_id not in self.emails:
            raise EmailNotFoundError(email_id)
        return self.emails[email_id]


class FakeNer:
    """Stands in for Presidio: finds the listed names wherever they occur."""

    def __init__(self, *names: str, fail: bool = False) -> None:
        self.names, self.fail = names, fail

    async def detect(self, text: str, language: str) -> list[Span]:
        if self.fail:
            raise PrivacyUnavailableError("Name detection failed")
        spans = []
        for name in self.names:
            start = text.find(name)
            while start >= 0:
                spans.append(Span(start, start + len(name), EntityKind.PERSON, name, Source.NER))
                start = text.find(name, start + 1)
        return spans
