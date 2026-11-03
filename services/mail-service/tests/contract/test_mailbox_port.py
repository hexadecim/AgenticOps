"""Liskov check: every mailbox adapter (fake, IMAP+SMTP, Gmail, Graph, and IMAP against the real GreenMail dev
mailbox when it is running) must pass the same behavioural suite for MailReader and MailSender."""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
import respx
from mail_service.adapters.outbound.providers.fake import FakeMailbox
from mail_service.adapters.outbound.providers.gmail import GmailMailbox
from mail_service.adapters.outbound.providers.graph import GraphMailbox
from mail_service.adapters.outbound.providers.imap import ImapMailbox
from mail_service.adapters.outbound.providers.mime import parse_mime
from mail_service.domain.mail import Address, IncomingMail, MailboxConfig, OutgoingMail
from mail_service.ports import MailReader, MailSender
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient

from ..conftest import imap_config, raw_mail
from .stubs import BASE_TIME, GmailStub, GraphStub, StubImapServer, StubSmtp

SINCE = BASE_TIME - timedelta(days=1)
DEVMAIL = ("localhost", 3143, 3025)


@dataclass
class Harness:
    reader: MailReader
    sender: MailSender
    address: str
    deliver: Callable[[bytes], Any]
    sent: Callable[[], Any]  # async → list[IncomingMail] of messages the sender sent


def _parse_all(raws: list[bytes]) -> list[IncomingMail]:
    return [parse_mime(r, provider_id=str(i)) for i, r in enumerate(raws)]


async def _sync(value: Any) -> Any:
    return value


def _devmail_running() -> bool:
    try:
        socket.create_connection(DEVMAIL[:2], timeout=0.3).close()
        return True
    except OSError:
        return False


@pytest.fixture(params=["fake", "imap", "gmail", "graph", "imap-greenmail"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    registry = ResilienceRegistry()
    kind = request.param
    if kind == "fake":
        box = FakeMailbox()
        yield Harness(
            box,
            box,
            box.address,
            lambda raw: box.deliver(raw, BASE_TIME),
            lambda: _sync(_parse_all(box.sent)),
        )
    elif kind == "imap":
        server, smtp = StubImapServer(), StubSmtp()
        imap = ImapMailbox(
            imap_config(), registry.get("mail-provider"), imap_factory=server.client, smtp_factory=smtp.client
        )
        yield Harness(imap, imap, "support@example.com", server.deliver, lambda: _sync(_parse_all(smtp.sent)))
    elif kind in ("gmail", "graph"):
        values = {"gmail_user": "support@example.com", "gmail_client_id": "cid"}
        values |= {
            "graph_tenant_id": "tenant",
            "graph_client_id": "cid",
            "graph_mailbox": "support@example.com",
        }
        secrets = {"gmail_client_secret": "s", "gmail_refresh_token": "r", "graph_client_secret": "s"}
        config = MailboxConfig(1, kind, "INBOX", 60, values, secrets)
        with respx.mock(assert_all_called=False) as router:
            http = ResilientHttpClient.from_registry(registry, "mail-provider")
            if kind == "gmail":
                stub: GmailStub | GraphStub = GmailStub(router)
                adapter: GmailMailbox | GraphMailbox = GmailMailbox(config, http)
            else:
                stub, adapter = GraphStub(router), GraphMailbox(config, http)
            yield Harness(
                adapter, adapter, "support@example.com", stub.deliver, lambda: _sync(_parse_all(stub.sent))
            )
            await http.aclose()
    else:
        if not _devmail_running():
            pytest.skip("GreenMail dev mailbox not running (make up)")
        # GreenMail creates a user on first delivery: create it, then run in a fresh, empty folder.
        user = f"contract-{uuid.uuid4().hex[:10]}@opspilot.local"
        with smtplib.SMTP(DEVMAIL[0], DEVMAIL[2], timeout=5) as smtp:
            smtp.sendmail("setup@opspilot.local", [user], b"Subject: setup\r\n\r\ncreates the user")
        await asyncio.sleep(0.3)
        admin = imaplib.IMAP4(DEVMAIL[0], DEVMAIL[1], timeout=5)
        admin.login(user, user)
        admin.create("Contract")
        values = imap_config(
            folder="Contract", imap_host=DEVMAIL[0], imap_port=DEVMAIL[1], imap_ssl=False, username=user,
            smtp_host=DEVMAIL[0], smtp_port=DEVMAIL[2],
        ).values  # fmt: skip
        imap = ImapMailbox(
            MailboxConfig(1, "imap", "Contract", 60, values, {"password": user}),
            registry.get("mail-provider"),
        )
        inbox = ImapMailbox(  # where the sender's mails to itself arrive
            MailboxConfig(1, "imap", "INBOX", 60, values, {"password": user}), registry.get("mail-provider")
        )
        baseline = (await inbox.fetch_new(None, limit=50, since=SINCE)).cursor

        def deliver(raw: bytes) -> None:
            admin.append("Contract", "", imaplib.Time2Internaldate(time.time()), raw)

        async def sent() -> list[IncomingMail]:
            for _ in range(30):
                batch = await inbox.fetch_new(baseline, limit=50, since=SINCE)
                if batch.mails:
                    return batch.mails
                await asyncio.sleep(0.1)
            return []

        yield Harness(imap, imap, user, deliver, sent)
        admin.logout()


async def _deliver(h: Harness, raw: bytes) -> None:
    result = h.deliver(raw)
    if asyncio.iscoroutine(result):
        await result


async def _fetch(h: Harness, cursor: Any, limit: int = 50) -> Any:
    for _ in range(20):  # a real server may take a moment to file an SMTP delivery
        batch = await h.reader.fetch_new(cursor, limit=limit, since=SINCE)
        if batch.mails or cursor is None:
            return batch
        await asyncio.sleep(0.1)
    return batch


async def test_empty_mailbox_yields_nothing_but_a_read_position(harness: Harness) -> None:
    batch = await harness.reader.fetch_new(None, limit=10, since=SINCE)
    assert batch.mails == [] and batch.more is False
    assert isinstance(batch.cursor, dict) and batch.cursor


async def test_reads_each_new_message_once_oldest_first(harness: Harness) -> None:
    first = await harness.reader.fetch_new(None, limit=10, since=SINCE)
    for subject in ("first", "second"):
        await _deliver(harness, raw_mail(subject, to=harness.address))
    batch = await _fetch(harness, first.cursor)
    assert [m.subject for m in batch.mails] == ["first", "second"]
    again = await harness.reader.fetch_new(batch.cursor, limit=10, since=SINCE)
    assert again.mails == []
    await _deliver(harness, raw_mail("third", to=harness.address))
    later = await _fetch(harness, again.cursor)
    assert [m.subject for m in later.mails] == ["third"]


async def test_mail_that_arrived_before_the_first_poll_is_read(harness: Harness) -> None:
    await _deliver(harness, raw_mail("waiting already", to=harness.address))
    batch = await harness.reader.fetch_new(None, limit=10, since=SINCE)
    assert [m.subject for m in batch.mails] == ["waiting already"]


async def test_limit_splits_reading_into_batches(harness: Harness) -> None:
    start = (await harness.reader.fetch_new(None, limit=10, since=SINCE)).cursor
    for n in range(3):
        await _deliver(harness, raw_mail(f"m{n}", to=harness.address))
    one = await harness.reader.fetch_new(start, limit=2, since=SINCE)
    assert [m.subject for m in one.mails] == ["m0", "m1"] and one.more is True
    two = await harness.reader.fetch_new(one.cursor, limit=2, since=SINCE)
    assert [m.subject for m in two.mails] == ["m2"] and two.more is False


async def test_parses_headers_body_threading_and_attachment_metadata(harness: Harness) -> None:
    raw = raw_mail(
        "Re: VPN down",
        to=harness.address,
        message_id="<Reply-1@kunde.de>",
        in_reply_to="<orig-1@kunde.de>",
        references=["<root@kunde.de>", "<orig-1@kunde.de>"],
        attachment=("error.pdf", b"%PDF-1.4 tiny"),
    )
    await _deliver(harness, raw)
    (mail,) = (await harness.reader.fetch_new(None, limit=10, since=SINCE)).mails
    assert mail.message_id == "reply-1@kunde.de"
    assert mail.sender == Address("jonas.weber@kunde.de", "Jonas Weber")
    assert mail.in_reply_to == "orig-1@kunde.de"
    assert mail.parent_ids() == ["orig-1@kunde.de", "root@kunde.de"]
    assert "nobody in Hamburg" in mail.body_text
    assert [(a.filename, a.size) for a in mail.attachments] == [("error.pdf", 13)]
    assert mail.provider_id and mail.received_at.tzinfo is not None


async def test_send_keeps_threading_headers_and_returns_the_message_id(harness: Harness) -> None:
    message_id = await harness.sender.send(
        OutgoingMail(
            to=(Address(harness.address, "Support"),),
            subject="Re: VPN down",
            body_text="We are looking into it.",
            in_reply_to="orig-1@kunde.de",
            references=("root@kunde.de",),
        )
    )
    (sent,) = await harness.sent()
    assert sent.message_id == message_id
    assert sent.subject == "Re: VPN down" and sent.in_reply_to == "orig-1@kunde.de"
    assert sent.references == ("root@kunde.de", "orig-1@kunde.de")
    assert "looking into it" in sent.body_text
