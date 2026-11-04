"""Answers to data requests: only the compliance lead's decision sends one, at most once, JSON attached."""

from __future__ import annotations

from datetime import UTC, datetime
from email import message_from_bytes

import opspilot_common.events.catalogue as c
from mail_service.adapters.outbound.memory import InMemoryStore, InMemoryUnitOfWork
from mail_service.adapters.outbound.providers.mime import build_mime
from mail_service.application import ActiveMailbox, QueueDsrMessage, SendDsrMessages
from mail_service.domain.errors import DetokeniseRefusedError, DetokeniseUnavailableError
from mail_service.domain.mail import Address, OutgoingAttachment, OutgoingMail
from mail_service.domain.outgoing import OutgoingStatus

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
MAIL = OutgoingMail(
    to=(Address("a@example.eu", ""),),
    subject="Your data request",
    body_text="Attached.",
    attachments=(OutgoingAttachment("dsr-0001.json", '{"ok": true}'),),
)


class Clock:
    def now(self) -> datetime:
        return NOW


class Source:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def outbound(self, dsr_id: str, kind: str) -> OutgoingMail:
        self.calls += 1
        if self.error:
            raise self.error
        return MAIL


class Sender:
    def __init__(self) -> None:
        self.sent: list[OutgoingMail] = []

    async def send(self, mail: OutgoingMail) -> str:
        self.sent.append(mail)
        return "id"


class Active(ActiveMailbox):
    def __init__(self, sender: Sender) -> None:
        super().__init__()
        self._sender = sender

    def current(self):  # type: ignore[no-untyped-def]
        class Connection:
            sender = self._sender

        return (object(), Connection())


def event(role: str = "compliance_lead", attempt: int = 1) -> c.DsrSendRequested:
    return c.DsrSendRequested(dsr_id="DSR-0001", kind="export", attempt=attempt, decided_by_role=role)


def setup(error: Exception | None = None):  # type: ignore[no-untyped-def]
    store, sender, source = InMemoryStore(), Sender(), Source(error)
    uow = lambda: InMemoryUnitOfWork(store)  # noqa: E731
    return (
        store,
        sender,
        source,
        QueueDsrMessage(uow, Clock()),
        SendDsrMessages(uow, Active(sender), source, Clock()),
    )


def results(store: InMemoryStore) -> list[c.DsrMessageResult]:
    return [p for p, _ in store.published if isinstance(p, c.DsrMessageResult)]


async def test_only_the_compliance_lead_can_start_a_send() -> None:
    _, sender, _, queue, send = setup()
    assert not await queue(event("support_agent"))
    assert not await queue(event("admin"))
    assert await send() == 0 and sender.sent == []


async def test_sent_once_with_the_result_announced() -> None:
    store, sender, _, queue, send = setup()
    assert await queue(event())
    assert not await queue(event())  # the event repeated
    assert await send() == 1
    assert await send() == 0
    assert sender.sent == [MAIL]
    assert [r.sent for r in results(store)] == [True]


async def test_a_retry_is_a_new_message() -> None:
    _, sender, _, queue, send = setup()
    await queue(event())
    await send()
    assert await queue(event(attempt=2))
    await send()
    assert len(sender.sent) == 2


async def test_privacy_down_waits_and_a_refusal_fails() -> None:
    store, sender, _, queue, send = setup(DetokeniseUnavailableError("down"))
    await queue(event())
    await send()
    assert results(store) == [] and sender.sent == []
    assert next(iter(store.dsr_messages.rows.values())).status is OutgoingStatus.PENDING

    store, sender, _, queue, send = setup(DetokeniseRefusedError("not waiting"))
    await queue(event())
    await send()
    assert [(r.sent, r.detail) for r in results(store)] == [(False, "not waiting")] and sender.sent == []


async def test_a_send_interrupted_by_a_crash_is_never_repeated() -> None:
    store, sender, _, queue, send = setup()
    await queue(event())
    next(iter(store.dsr_messages.rows.values())).status = OutgoingStatus.SENDING
    assert await send.recover() == 1
    assert await send() == 0 and sender.sent == []
    assert [r.sent for r in results(store)] == [False]


def test_the_json_is_attached() -> None:
    raw, _ = build_mime(MAIL, "support@opspilot.local")
    parts = list(message_from_bytes(raw).walk())
    attached = [p for p in parts if p.get_filename()]
    assert (
        attached[0].get_filename() == "dsr-0001.json" and attached[0].get_content_type() == "application/json"
    )
    assert b'{"ok": true}' in attached[0].get_payload(decode=True)
