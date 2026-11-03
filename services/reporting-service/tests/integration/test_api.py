from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
    ]
    for event in events:
        await handler(event)
    await handler(events[-3])  # redelivered: ignored by the inbox
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
    ]
    again = (await client.get("/v1/incidents/KAN-19/timeline", headers=user())).json()
    assert len(again["entries"]) == 9
    assert (await client.get("/v1/incidents/KAN-404/timeline", headers=user())).json()["entries"] == []
