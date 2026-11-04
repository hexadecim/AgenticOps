"""The poller and the approval queue at volume, on real Postgres, IMAP and SMTP. Synthetic mail and people.

Limits below are what a person would notice on the reference PC, not a benchmark: the queue is opened every minute by
every approver, and a poll cycle must not fall behind the mailbox.
"""

from __future__ import annotations

import asyncio
import smtplib
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from approval_service.bootstrap import build_container as build_approval
from approval_service.config import Settings as ApprovalSettings
from approval_service.main import create_app as create_approval_app
from conftest import POSTGRES
from mail_service.adapters.outbound.sql import MailTables
from mail_service.application.polling import Outcome
from mail_service.bootstrap import build_container as build_mail
from mail_service.config import Settings as MailSettings
from mail_service.domain.mail import MailboxConfig
from opspilot_common.auth import Principal, Role, TokenIssuer
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SECRET = "load-test-secret-at-least-32-characters"
ADDRESS = f"load-{uuid.uuid4().hex[:8]}@opspilot.local"
MAILS = 1000
BASELINE_EMAILS = 20_000
PENDING = 5_000
DECIDED = 20_000


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


# ---------------------------------------------------------------- poller
class Source:
    async def mailbox(self) -> MailboxConfig:
        return MailboxConfig(
            version=1,
            provider="imap",
            folder="INBOX",
            poll_interval_seconds=30,
            values={
                "provider": "imap",
                "imap_host": "localhost",
                "imap_port": 53143,
                "imap_ssl": False,
                "username": ADDRESS,
                "smtp_host": "localhost",
                "smtp_port": 53025,
            },
            secrets={"password": ADDRESS},
        )


def _deliver(n: int) -> None:
    message = EmailMessage()
    message["From"] = f"Synthetic Person {n} <person{n}@example.test>"
    message["To"] = ADDRESS
    message["Subject"] = f"Uploads fail at site {n % 50}"
    message["Message-ID"] = f"<load-{ADDRESS}-{n}@example.test>"
    message.set_content("Uploads fail with error 503.\n" * 40)
    with smtplib.SMTP("127.0.0.1", 53025) as smtp:
        smtp.send_message(message)


@pytest.fixture
async def mail_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"postgresql+asyncpg://mail_svc:mail_dev_password@{POSTGRES}", pool_size=10)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE mail.emails, mail.poll_state, mail.outbox, mail.inbox, mail.erased_emails CASCADE")
        )
    yield engine
    await engine.dispose()


async def _baseline_emails(engine: AsyncEngine, count: int) -> None:
    now = datetime.now(UTC)
    insert = text(
        """
        INSERT INTO mail.emails (id, dedupe_key, thread_id, provider, provider_id, message_id, "references", subject,
          sender, recipients, body_text, attachments, received_at, fetched_at)
        SELECT gen_random_uuid()::text, 'baseline-' || n, gen_random_uuid()::text, 'imap', n::text,
          '<baseline-' || n || '@example.test>', '[]', 'Old', '{"address":"x@example.test"}', '[]',
          repeat('body ', 200), '[]', :now, :now
        FROM generate_series(1, :count) n
        """
    )
    async with engine.begin() as connection:
        await connection.execute(insert, {"now": now, "count": count})
        await connection.execute(text("ANALYZE mail.emails"))


async def test_poller_stores_a_large_backlog_once_and_keeps_up(mail_engine: AsyncEngine) -> None:
    await _baseline_emails(mail_engine, BASELINE_EMAILS)
    with ThreadPoolExecutor(16) as pool:
        started = time.monotonic()
        list(pool.map(_deliver, range(MAILS)))
        print(f"\ndelivered {MAILS} mails over SMTP in {time.monotonic() - started:.1f} s")

    container = build_mail(
        MailSettings(jwt_secret=SECRET, log_json=False),
        engine=mail_engine,
        schema="mail",
        redis=fakeredis.aioredis.FakeRedis(),
        source=Source(),
    )
    tables = MailTables(schema="mail")
    durations: list[float] = []
    started = time.monotonic()
    while True:
        cycle = time.monotonic()
        result = await container.poll()
        assert result.outcome is Outcome.POLLED, result
        durations.append(time.monotonic() - cycle)
        if result.new == 0:
            break
    elapsed = time.monotonic() - started

    async with container.sessions() as session:
        stored = await session.scalar(
            select(func.count())
            .select_from(tables.emails)
            .where(tables.emails.c.dedupe_key.not_like("baseline-%"))
        )
        events = await session.scalar(select(func.count()).select_from(tables.outbox))
    rate = MAILS / elapsed
    print(
        f"poller: {stored} mails in {elapsed:.1f} s ({rate:.1f}/s) on top of {BASELINE_EMAILS} stored, "
        f"{len(durations)} cycles, cycle p50 {statistics.median(durations):.2f} s, max {max(durations):.2f} s, "
        f"{events} events"
    )
    assert stored == MAILS
    assert events == MAILS, "one email.received per mail"
    # A cycle takes up to 100 mails. At the shortest interval (30 s) that is 3.3 mails/s sustained.
    assert max(durations) < 30, "a poll cycle must finish within the shortest poll interval"

    async with container.sessions() as session, session.begin():
        await session.execute(text("DELETE FROM mail.poll_state"))
    while (await container.poll()).new:
        pass
    async with container.sessions() as session:
        again = await session.scalar(select(func.count()).select_from(tables.outbox))
    assert again == MAILS, "reading the mailbox again must not create a second copy"
    print("poller: reset of the read position added no copies")


# ---------------------------------------------------------------- approval queue
@pytest.fixture
async def approval_api() -> AsyncIterator[tuple[httpx.AsyncClient, Any, AsyncEngine]]:
    engine = create_async_engine(
        f"postgresql+asyncpg://approval_svc:approval_dev_password@{POSTGRES}", pool_size=20, max_overflow=20
    )
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE approval.approvals, approval.outbox, approval.inbox CASCADE"))
    container = build_approval(
        ApprovalSettings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="approval",
        redis=fakeredis.aioredis.FakeRedis(),
    )
    app = (
        create_approval_app(container, with_events=False)
        if _takes_with_events()
        else create_approval_app(container)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://load", timeout=60) as client:
        yield client, container, engine
    await engine.dispose()


def _takes_with_events() -> bool:
    import inspect

    return "with_events" in inspect.signature(create_approval_app).parameters


async def _fill_approvals(engine: AsyncEngine) -> datetime:
    now = datetime.now(UTC).replace(microsecond=0)
    proposal = '{"note": "' + "x" * 2000 + '"}'
    sql = text(
        """
        INSERT INTO approval.approvals (id, run_id, thread_id, gate, status, title, detail, site, severity, proposal,
          edits, requested_at, decided_at, decided_by, decided_by_role, reminders)
        SELECT gen_random_uuid()::text, gen_random_uuid()::text, gen_random_uuid()::text,
          (ARRAY['ticket','fix','message'])[1 + n % 3], :status, 'Case ' || n, 'detail', 'Berlin',
          CASE WHEN n % 50 = 0 THEN 'sev1' ELSE 'sev3' END, CAST(:proposal AS json), CAST('{}' AS json),
          CAST(:base AS timestamptz) - make_interval(secs => CAST(:count AS int) - n),
          CAST(:decided_at AS timestamptz) + make_interval(secs => n * CAST(:step AS int)),
          CAST(:by AS text), CAST(:role AS text), 0
        FROM generate_series(1, :count) n
        """
    )
    pending = {
        "status": "pending", "proposal": proposal, "base": now, "count": PENDING, "decided_at": None, "step": 0,
        "by": None, "role": None,
    }  # fmt: skip
    decided = {
        **pending, "status": "approved", "count": DECIDED, "decided_at": now - timedelta(days=30), "step": 1,
        "by": "u-1", "role": "l1_analyst", "base": now - timedelta(days=60),
    }  # fmt: skip
    async with engine.begin() as connection:
        await connection.execute(sql, pending)
        await connection.execute(sql, decided)
        await connection.execute(text("ANALYZE approval.approvals"))
    return now


def _session(*roles: Role) -> dict[str, str]:
    token = TokenIssuer(SECRET).issue(Principal("u-load", "Load", frozenset(roles), mfa=True))
    return {"Authorization": f"Bearer {token}"}


async def _timed_gets(client: httpx.AsyncClient, path: str, concurrency: int, total: int) -> list[float]:
    headers = _session(Role.SUPPORT_AGENT)
    gate = asyncio.Semaphore(concurrency)
    durations: list[float] = []

    async def one() -> None:
        async with gate:
            started = time.monotonic()
            response = await client.get(path, headers=headers)
            durations.append(time.monotonic() - started)
            assert response.status_code == 200, response.text[:200]

    await asyncio.gather(*(one() for _ in range(total)))
    return durations


async def test_approval_queue_stays_correct_and_quick_with_a_long_backlog(
    approval_api: tuple[httpx.AsyncClient, Any, AsyncEngine],
) -> None:
    client, _, engine = approval_api
    await _fill_approvals(engine)

    first = await client.get("/v1/approvals", headers=_session(Role.SUPPORT_AGENT))
    assert first.status_code == 200, first.text[:300]
    body = first.json()
    stats = body["stats"]
    print(
        f"\nqueue: {stats['waiting']} waiting (true {PENDING}), {len(body['items'])} rows returned, "
        f"{len(first.content) / 1024:.0f} KiB"
    )
    assert stats["waiting"] == PENDING, "the count must be the true number, not the size of one page"
    assert sum(stats["waiting_by_gate"].values()) == PENDING
    assert stats["sev1_waiting"] == PENDING // 50

    decided = await client.get("/v1/approvals?decided=true", headers=_session(Role.SUPPORT_AGENT))
    moments = [a["decided_at"] for a in decided.json()["items"]]
    assert moments == sorted(moments, reverse=True), "newest decision first"
    assert moments[0] > (datetime.now(UTC) - timedelta(days=30)).isoformat() and _is_latest(moments[0])

    durations = await _timed_gets(client, "/v1/approvals", concurrency=20, total=200)
    p50, p95 = statistics.median(durations), percentile(durations, 0.95)
    print(f"queue: 200 reads, 20 at a time: p50 {p50 * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms")
    assert p95 < 1.5, "opening the queue must feel immediate under load"

    durations = await _timed_gets(client, "/v1/approvals?decided=true", concurrency=20, total=100)
    p95 = percentile(durations, 0.95)
    print(f"decided: 100 reads, 20 at a time: p95 {p95 * 1000:.0f} ms")
    assert p95 < 1.5


def _is_latest(moment: str) -> bool:
    # The newest seeded decision is DECIDED seconds after the start of the decided window.
    newest = datetime.now(UTC) - timedelta(days=30) + timedelta(seconds=DECIDED)
    return datetime.fromisoformat(moment) > newest - timedelta(minutes=30)


async def test_many_approvers_deciding_at_once_each_approval_is_decided_once(
    approval_api: tuple[httpx.AsyncClient, Any, AsyncEngine],
) -> None:
    client, _, engine = approval_api
    await _fill_approvals(engine)
    rows = (await client.get("/v1/approvals?gate=ticket", headers=_session(Role.L1_ANALYST))).json()["items"][
        :100
    ]
    ids = [row["id"] for row in rows]

    async def decide(approval_id: str, decision: str, role: Role) -> int:
        body = {"decision": decision}
        if decision == "reject":
            body["reason"] = "Not a real case"
        response = await client.post(
            f"/v1/approvals/{approval_id}/decision", json=body, headers=_session(role)
        )
        return response.status_code

    started = time.monotonic()
    # Three people on one approval at the same moment: a second approves, a third disagrees.
    groups = await asyncio.gather(
        *(
            asyncio.gather(
                decide(i, "approve", Role.L1_ANALYST),
                decide(i, "approve", Role.SUPPORT_AGENT),
                decide(i, "reject", Role.SUPPORT_MANAGER),
            )
            for i in ids[:10]
        )
    )
    codes = [code for group in groups for code in group]
    elapsed = time.monotonic() - started
    ids = ids[:10]
    async with engine.connect() as connection:
        events = (
            await connection.execute(
                text("SELECT body::json->'payload'->>'approval_id', count(*) FROM approval.outbox GROUP BY 1")
            )
        ).all()
    print(
        f"\n{len(codes)} concurrent decisions on {len(ids)} approvals in {elapsed:.1f} s; "
        f"codes {sorted({c for c in codes})}"
    )
    assert all(c < 500 for c in codes), "a conflict is an answer, never a server error"
    counts = {row[0]: row[1] for row in events}
    assert set(ids) <= set(counts)
    assert max(counts[i] for i in ids) == 1, "exactly one approval.decided per approval"
