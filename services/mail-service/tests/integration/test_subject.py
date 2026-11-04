"""mail-service's part of a data request, on SQL: finding a person's mail by address, erasing it for good."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import opspilot_common.events.catalogue as c
import pytest
from mail_service.adapters.outbound.sql import MailTables, SqlEmailRepository
from mail_service.adapters.outbound.subject import SqlMailSubjectStore
from opspilot_common.subjects import SubjectQuery
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def _email(email_id: str, sender: str) -> dict[str, object]:
    return {
        "id": email_id,
        "dedupe_key": f"key-{email_id}",
        "thread_id": "t",
        "provider": "imap",
        "provider_id": email_id,
        "references": [],
        "subject": f"subject {email_id}",
        "sender": {"email": sender, "name": "X"},
        "recipients": {},
        "body_text": f"body {email_id}",
        "attachments": [],
        "received_at": NOW,
        "fetched_at": NOW,
    }


@pytest.fixture
async def world() -> AsyncIterator[tuple[SqlMailSubjectStore, async_sessionmaker[AsyncSession], MailTables]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = MailTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    async with sessions() as s, s.begin():
        await s.execute(
            insert(tables.emails),
            [
                _email("own", "Alex@Example.eu"),
                _email("other", "alex@example.eu.evil.org"),
                _email("x", "b@c.de"),
            ],
        )
    yield SqlMailSubjectStore(sessions, tables), sessions, tables
    await engine.dispose()


async def test_finds_their_mail_by_address_any_case_and_no_lookalike(world) -> None:  # type: ignore[no-untyped-def]
    store, _, _ = world
    report = await store.report(SubjectQuery(address="alex@example.eu", tokens=["EMAIL_1"]))
    assert report.email_ids == ["own"] and report.count == 1 and report.items == []
    full = await store.report(SubjectQuery(address="alex@example.eu", include_items=True))
    assert full.items[0]["body"] == "body own"


async def test_a_mention_is_counted_but_not_copied(world) -> None:  # type: ignore[no-untyped-def]
    store, _, _ = world
    report = await store.report(
        SubjectQuery(own_email_ids=["own"], mentioned_email_ids=["x"], include_items=True)
    )
    assert report.count == 1 and "named in 1 other email" in report.what
    assert "body x" not in str(report.items)


async def test_erased_mail_is_gone_and_cannot_be_fetched_again(world) -> None:  # type: ignore[no-untyped-def]
    store, sessions, tables = world
    request = c.SubjectErasureRequested(
        dsr_id="DSR-0001",
        tokens=["EMAIL_1"],
        own_email_ids=["own"],
        run_ids=[],
        decided_by_role="compliance_lead",
    )
    await store.erase(request)
    async with sessions() as s:
        ids = {r.id for r in await s.execute(select(tables.emails.c.id))}
        acks = list(await s.execute(select(tables.outbox.c.type, tables.outbox.c.body)))
        hashes = list(await s.execute(select(tables.erased_emails.c.key_hash)))
        repo = SqlEmailRepository(s, tables)
        assert await repo.exists("key-own")  # the poller treats it as already seen
        assert not await repo.exists("key-new")
    assert ids == {"other", "x"} and len(hashes) == 1
    assert acks[0].type == "subject.store_erased" and "The mailbox itself" in acks[0].body
    assert "key-own" not in str(hashes[0])
