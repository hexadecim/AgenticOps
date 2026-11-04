from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import httpx
import opspilot_common.events.catalogue as c
import pytest
from opspilot_common.auth import Principal, Role, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from reporting_service.adapters.inbound.events import on_any_event
from reporting_service.bootstrap import build_container
from reporting_service.config import Settings
from reporting_service.main import create_app
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

SECRET = "reporting-test-jwt-secret-at-least-32-chars"
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


def at(
    minutes: float, payload: c.EventPayload, source: str = "svc", actor: str | None = None
) -> EventEnvelope:
    env = EventEnvelope.wrap(payload, source=source, actor=actor)
    return env.model_copy(update={"occurred_at": T0 + timedelta(minutes=minutes)})


def run(minutes: float, status: str, n: int = 1, **extra: str) -> EventEnvelope:
    return at(
        minutes,
        c.RunStatusChanged(run_id=f"r-{n}", thread_id=f"t-{n}", status=status, email_id=f"e-{n}", **extra),
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        Settings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    handler = Inbox(container.tables.inbox, "reporting-service.all").guard(
        container.sessions, on_any_event(container.sessions, container.tables)
    )
    request = c.ApprovalRequested(
        approval_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        gate=c.Gate.TICKET,
        title="Upload stuck at 0%",
        detail="d",
        site="SITE-A014",
        severity="sev2",
    )
    events = [
        run(0.1, "queued"),  # before the email event: arrives out of order
        at(
            0,
            c.EmailReceived(email_id="e-1", thread_id="t-1", provider="imap", received_at=T0),
            "mail-service",
        ),
        at(0.2, c.PrivacyRecorded(action="pseudonymise", detail="PERSON (2) tokenised", thread_id="t-1")),
        run(0.3, "waiting_llm"),
        at(
            0.4,
            c.BreakerStateChanged(
                service="agent-orchestrator", dependency="ollama", old="closed", new="open"
            ),
        ),
        at(
            1.5,
            c.BreakerStateChanged(
                service="agent-orchestrator", dependency="ollama", old="open", new="closed"
            ),
        ),
        at(
            2,
            c.AgentToolCalled(
                run_id="r-1", thread_id="t-1", agent="Intake", tool="classify", detail="Upload, sev2"
            ),
        ),
        at(2.1, request),
        run(2.1, "waiting_approval"),
        at(
            3,
            c.ApprovalDecided(
                approval_id="a-1",
                run_id="r-1",
                thread_id="t-1",
                gate=c.Gate.TICKET,
                decision=c.Decision.APPROVE,
                decided_by="u-1",
                decided_by_role="l1_analyst",
            ),
        ),
        at(
            3.1,
            c.JiraIssueCreated(
                approval_id="a-1",
                thread_id="t-1",
                issue_key="KAN-19",
                project="KAN",
                run_id="r-1",
                severity="sev1",
            ),
        ),
        run(3.1, "ticket_created", jira_key="KAN-19"),
        run(5, "queued", n=2),
        run(6, "ignored", n=2),
        # Scheduled probe sweep (no run): a system change; an older "healthy" arrives after the failure.
        at(4, c.ProbeStatus(probe="Upload", ok=False, detail="timeout after 30 s", region="eu-west-1")),
        at(3.5, c.ProbeStatus(probe="Upload", ok=True, detail="200 in 210 ms", region="eu-west-1")),
        at(3.6, c.ProbeStatus(probe="Portal", ok=True, detail="200 in 180 ms", latency_ms=180)),
    ]
    for event in events:
        await handler(event)
    await handler(events[-6])  # redelivered: ignored by the inbox
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://reporting") as http:
        yield http
    await engine.dispose()


def user() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + TokenIssuer(SECRET).issue(Principal("u", "L", frozenset({Role.L1_ANALYST})))
    }


async def test_dashboard_from_events(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/dashboard")).status_code == 401
    d = (await client.get("/v1/dashboard", headers=user())).json()

    deps = {x["name"]: x for x in d["dependencies"]}
    assert deps["ollama"]["health"] == "up" and deps["ollama"]["callers"] == {"agent-orchestrator": "closed"}
    assert deps["jira"]["health"] == "up" and deps["jira"]["since"] is None  # no failures recorded

    probes = {(x["name"], x["region"]): x for x in d["probes"]}
    assert set(probes) == {("Upload", "eu-west-1"), ("Portal", None)}
    upload = probes[("Upload", "eu-west-1")]
    assert upload["health"] == "down" and upload["detail"] == "timeout after 30 s"  # older "ok" ignored
    assert upload["since"] == (T0 + timedelta(minutes=4)).isoformat()
    assert probes[("Portal", None)]["health"] == "up" and probes[("Portal", None)]["latency_ms"] == 180

    stages = {s["id"]: s["count"] for s in d["stages"]}
    assert stages["engineering"] == 1 and stages["approval"] == 0 and stages["intake"] == 0

    levels = {s["id"]: s["value"] for s in d["service_levels"]}
    assert levels["email_to_ticket"] == "3m 06s"  # email received (0) → KAN-19 created (3.1 min)
    assert levels["email_to_request"] == "2m 06s" and levels["approval_wait"] == "54s"
    assert levels["ignored"] == "1" and levels["ai_paused"] == "1"

    (incident,) = d["incidents"]
    assert (
        incident["key"] == "KAN-19"
        and incident["title"] == "Upload stuck at 0%"
        and incident["severity"] == "sev1"  # the approver's severity, not the AI's sev2
    )
    lines = [(e["actor"], e["text"]) for e in d["timeline"]["entries"]]
    assert lines == [
        ("Mailbox", "Email received via IMAP"),
        ("Privacy gate", "PERSON (2) tokenised"),
        ("Orchestrator", "AI paused: waiting for the local model"),
        ("Agents", "Local AI model failing: calls paused"),
        ("Agents", "Local AI model recovered"),
        ("Intake", "classify: Upload, sev2"),
        ("Triage", "Approval requested (ticket): Upload stuck at 0%"),
        ("L1 Analyst", "Approved the ticket request"),
        ("Jira connector", "KAN-19 created"),
        ("Health", "Upload (eu-west-1) probe healthy: 200 in 210 ms"),
        ("Health", "Portal probe healthy: 200 in 180 ms"),
        ("Health", "Upload (eu-west-1) probe failing: timeout after 30 s"),
    ]
    again = (await client.get("/v1/incidents/KAN-19/timeline", headers=user())).json()
    assert len(again["entries"]) == 12
    assert (await client.get("/v1/incidents/KAN-404/timeline", headers=user())).json()["entries"] == []


async def test_verdict_outlives_the_customer_message_and_follow_ups_wait_for_the_reply() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        Settings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    handle = Inbox(container.tables.inbox, "reporting-service.all").guard(
        container.sessions, on_any_event(container.sessions, container.tables)
    )
    for envelope in (
        run(1, "resolved", node="health_check"),
        run(2, "waiting_approval", node="gate_message"),
        run(3, "resolved", node="await_send"),
        run(1, "waiting_customer", n=2),
    ):
        await handle(envelope)
    t = container.tables

    async def status_of(run_id: str) -> Any:
        async with container.sessions() as session:
            return (await session.execute(select(t.cases).where(t.cases.c.run_id == run_id))).one()

    fixed, asking = await status_of("r-1"), await status_of("r-2")
    assert fixed.verdict == "resolved" and fixed.status == "resolved"
    # When it was fixed, not when the message went out.
    assert fixed.verdict_at.replace(tzinfo=UTC) == T0 + timedelta(minutes=1)
    assert asking.status == "waiting_customer"
    async with container.sessions() as session:
        notes = (await session.execute(select(t.timeline.c.text))).scalars().all()
    assert (
        sum(n.startswith("Resolved:") for n in notes) == 1
    )  # the repeat at the run's end adds no second note

    await handle(at(5, c.EmailReceived(email_id="e-9", thread_id="t-2", provider="imap", received_at=T0)))
    assert (await status_of("r-2")).status == "customer_replied"
    await engine.dispose()
