"""Approved message → recipients: the gate, detokenising, retries, and never sending twice."""

from __future__ import annotations

from datetime import timedelta
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest
from mail_service.adapters.outbound.memory import InMemoryStore, InMemoryUnitOfWork
from mail_service.adapters.outbound.providers.fake import FakeMailbox
from mail_service.application import (
    ActiveMailbox,
    PurgeExpiredEmails,
    QueueMessage,
    RecordMessageApproval,
    SendDueMessages,
)
from mail_service.domain.errors import (
    DetokeniseRefusedError,
    DetokeniseUnavailableError,
    MailboxRejectedError,
    MailboxUnavailableError,
)
from mail_service.domain.mail import Address, IncomingMail, StoredEmail
from mail_service.domain.outgoing import OutgoingStatus
from mail_service.ports import MailboxConnection
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    Decision,
    Gate,
    MessageFailed,
    MessageRecipient,
    MessageSendRequested,
    MessageSent,
)

from ..conftest import NOW, imap_config

VAULT = {
    "[EMAIL_1]": "anna@example.eu",
    "[PERSON_1]": "Anna Muster",
    "[EMAIL_4]": "ben@example.eu",
    "[EMAIL_7]": "gone@example.eu",
}
BODY = "Hello [RECIPIENT_NAME],\n\nFixed.\n\nOpsPilot Support"


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> Any:
        return self.at


class Privacy:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[str] = []

    async def detokenise(self, text: str, *, thread_id: str) -> str:
        self.calls.append(text)
        if self.error:
            raise self.error
        for token, value in VAULT.items():
            text = text.replace(token, value)
        return text


def stored(email_id: str, address: str, message_id: str) -> StoredEmail:
    mail = IncomingMail(
        provider_id=email_id, message_id=message_id, subject="Upload stuck", sender=Address(address),
        received_at=NOW, body_text="help", references=("root@kunde.de",),
    )  # fmt: skip
    return StoredEmail(email_id, "t-1", "imap", NOW, mail)


class World:
    def __init__(self) -> None:
        self.store = InMemoryStore()
        self.clock = Clock()
        self.privacy = Privacy()
        self.box = FakeMailbox("support@opspilot.local")
        self.active = ActiveMailbox()
        self.active.config = imap_config()

        async def close() -> None:
            return None

        self.active.connection = MailboxConnection("imap", self.box, self.box, close)

        def uow() -> InMemoryUnitOfWork:
            return InMemoryUnitOfWork(self.store)

        self.queue = QueueMessage(uow, self.clock)  # type: ignore[arg-type]
        self.approval = RecordMessageApproval(uow, self.clock)  # type: ignore[arg-type]
        self.send = SendDueMessages(uow, self.active, self.privacy, self.clock)  # type: ignore[arg-type]
        self.purge = PurgeExpiredEmails(uow, self.clock)  # type: ignore[arg-type]
        self.store.emails.rows["e-1"] = stored("e-1", "anna@example.eu", "first@kunde.de")
        self.store.emails.rows["e-4"] = stored("e-4", "BEN@example.eu", "second@kunde.de")

    def request(self, *who: tuple[str, str | None, str]) -> MessageSendRequested:
        return MessageSendRequested(
            message_id="m-1", run_id="r-1", thread_id="t-1", approval_id="a-1",
            recipient_tokens=[n or e for e, n, _ in who], subject="Re: Upload stuck [KAN-12]", body=BODY,
            jira_key="KAN-12", kind="resolution",
            recipients=[MessageRecipient(email_token=e, name_token=n, email_id=i) for e, n, i in who],
        )  # fmt: skip

    async def approve(
        self, role: str = "l1_analyst", gate: Gate = Gate.MESSAGE, d: Decision = Decision.APPROVE
    ) -> bool:
        return await self.approval(
            ApprovalDecided(
                approval_id="a-1", run_id="r-1", thread_id="t-1", gate=gate, decision=d, decided_by="u-1",
                decided_by_role=role,
            )
        )  # fmt: skip

    def sent(self) -> list[Message]:
        return [message_from_bytes(raw) for raw in self.box.sent]

    def events(self, kind: type) -> list[Any]:
        return [p for p, _ in self.store.published if isinstance(p, kind)]

    def rows(self) -> list[Any]:
        return list(self.store.outgoing.rows.values())


ANNA = ("EMAIL_1", "PERSON_1", "e-1")
BEN = ("EMAIL_4", None, "e-4")


@pytest.fixture
def world() -> World:
    return World()


async def test_nothing_is_sent_without_an_approval_seen_by_mail_service(world: World) -> None:
    await world.queue(world.request(ANNA))
    assert await world.send() == 0 and not world.box.sent

    assert not await world.approve(role="admin")  # not allowed to decide T3
    assert not await world.approve(role="nonsense")
    assert not await world.approve(gate=Gate.FIX)
    assert not await world.approve(d=Decision.REJECT)
    assert await world.send() == 0 and not world.box.sent

    assert await world.approve()
    assert await world.send() == 1 and len(world.box.sent) == 1


async def test_approval_may_arrive_before_the_request(world: World) -> None:
    await world.approve()
    await world.queue(world.request(ANNA))
    assert await world.send() == 1


async def test_each_recipient_gets_their_own_reply_with_real_values(world: World) -> None:
    assert await world.queue(world.request(ANNA, BEN))
    assert not await world.queue(world.request(ANNA, BEN))  # repeat of the event: nothing added
    await world.approve()
    await world.send()

    anna, ben = world.sent()
    assert anna["To"] == "Anna Muster <anna@example.eu>" and ben["To"] == "ben@example.eu"
    assert anna["In-Reply-To"] == "<first@kunde.de>" and ben["In-Reply-To"] == "<second@kunde.de>"
    assert "<root@kunde.de>" in anna["References"]
    assert anna["Subject"] == "Re: Upload stuck [KAN-12]"
    anna_text = anna.get_payload(decode=True).decode()  # type: ignore[union-attr]
    ben_text = ben.get_payload(decode=True).decode()  # type: ignore[union-attr]
    assert "Hello Anna Muster," in anna_text and "Hello," in ben_text
    assert "[" not in anna_text and "[" not in ben_text and "Anna" not in ben_text

    assert all(r.status is OutgoingStatus.SENT for r in world.rows())
    assert all(
        "anna@example.eu" not in r.body and "Anna" not in r.body for r in world.rows()
    )  # still tokenised
    (done,) = world.events(MessageSent)
    assert done.recipient_tokens == ["PERSON_1", "EMAIL_4"] and done.failed_tokens == []
    assert done.approval_id == "a-1" and done.run_id == "r-1" and done.jira_key == "KAN-12"
    assert await world.send() == 0  # nothing goes out twice


async def test_only_the_sender_of_the_answered_email_can_receive_it(world: World) -> None:
    await world.queue(world.request(("EMAIL_7", None, "e-1")))  # a token for someone else, on Anna's email
    await world.approve()
    await world.send()
    assert not world.box.sent
    (failed,) = world.events(MessageFailed)
    assert "not the sender" in failed.reason and failed.failed_tokens == ["EMAIL_7"]


async def test_original_email_gone_after_retention_fails_the_row(world: World) -> None:
    await world.queue(world.request(("EMAIL_1", None, "e-gone")))
    await world.approve()
    await world.send()
    assert not world.box.sent and world.events(MessageFailed)


async def test_privacy_down_waits_with_backoff_and_never_sends_tokens(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    world.privacy.error = DetokeniseUnavailableError("privacy-service is unavailable")
    await world.send()
    assert not world.box.sent and not world.events(MessageSent) and not world.events(MessageFailed)
    (row,) = world.rows()
    assert row.status is OutgoingStatus.PENDING and row.next_try_at == NOW + timedelta(seconds=60)
    assert await world.send() == 0  # not due yet

    world.privacy.error = None
    world.clock.at = NOW + timedelta(seconds=61)
    assert await world.send() == 1 and len(world.box.sent) == 1 and world.events(MessageSent)


async def test_unknown_token_or_refused_recipient_fails_for_good(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    world.privacy.error = DetokeniseRefusedError("Unknown token")
    await world.send()
    (failed,) = world.events(MessageFailed)
    assert failed.reason == "Unknown token" and await world.send() == 0


async def test_partial_failure_is_reported_as_sent_with_the_missing_ones(world: World) -> None:
    await world.queue(world.request(ANNA, BEN))
    await world.approve()
    original = world.box.send
    calls = 0

    async def second_refused(mail: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MailboxRejectedError("The SMTP server refused the recipients.")
        return await original(mail)

    world.box.send = second_refused  # type: ignore[method-assign]
    await world.send()
    (done,) = world.events(MessageSent)
    assert done.recipient_tokens == ["PERSON_1"] and done.failed_tokens == ["EMAIL_4"]
    assert done.detail == "The SMTP server refused the recipients."
    assert not world.events(MessageFailed)


async def test_mailbox_down_retries_then_gives_up_after_a_day(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    world.box.fail_with = MailboxUnavailableError("circuit open", retry_after=300)
    await world.send()
    (row,) = world.rows()
    assert row.status is OutgoingStatus.PENDING and row.next_try_at == NOW + timedelta(seconds=300)

    world.clock.at = NOW + timedelta(hours=25)
    await world.send()
    (failed,) = world.events(MessageFailed)
    assert failed.reason.startswith("Gave up after 24 hours") and not world.box.sent


async def test_a_send_interrupted_by_a_crash_is_never_repeated(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    (row,) = world.rows()
    row.status = OutgoingStatus.SENDING
    assert await world.send.recover() == 1
    assert await world.send() == 0 and not world.box.sent
    (failed,) = world.events(MessageFailed)
    assert "may have gone out" in failed.reason


async def test_unexpected_error_during_send_is_not_retried(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()

    async def boom(_: Any) -> str:
        raise RuntimeError("boom")

    world.box.send = boom  # type: ignore[method-assign]
    await world.send()
    (row,) = world.rows()
    assert row.status is OutgoingStatus.FAILED and world.events(MessageFailed)


async def test_no_mailbox_waits_and_a_mailbox_that_cannot_send_fails(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    connection = world.active.connection
    world.active.connection = None
    assert await world.send() == 0 and world.rows()[0].status is OutgoingStatus.PENDING

    assert connection is not None
    connection.sender = None
    world.active.connection = connection
    await world.send()
    (failed,) = world.events(MessageFailed)
    assert "cannot send" in failed.reason


async def test_retention_removes_only_finished_rows(world: World) -> None:
    await world.queue(world.request(ANNA))
    await world.approve()
    await world.send()
    world.clock.at = NOW + timedelta(days=31)
    await world.purge()
    assert not world.rows()
