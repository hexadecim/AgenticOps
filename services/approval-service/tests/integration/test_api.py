from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import httpx
import pytest
from approval_service.adapters.inbound.events import on_event
from approval_service.bootstrap import Container, build_container
from approval_service.config import Settings
from approval_service.main import create_app
from opspilot_common.auth import Principal, Role, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import ApprovalRequested, Gate, JiraIssueCreated
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

SECRET = "approval-test-jwt-secret-at-least-32-chars"
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


def user(*roles: Role, uid: str = "u-1") -> dict[str, str]:
    return {"Authorization": "Bearer " + TokenIssuer(SECRET).issue(Principal(uid, "Lena", frozenset(roles)))}


def requested(
    n: int, gate: Gate = Gate.TICKET, severity: str = "sev2", minutes_ago: int = 0
) -> EventEnvelope:
    env = EventEnvelope.wrap(
        ApprovalRequested(
            approval_id=f"a-{n}",
            run_id=f"r-{n}",
            thread_id=f"t-{n}",
            gate=gate,
            title=f"Problem {n}",
            detail="Intake: upload failure · no related open ticket",
            site="SITE-A014",
            severity=severity,
            proposal={
                "ticket": {"summary": f"[SITE-A014] Problem {n}"},
                "email": {"body": "Hello [PERSON_1]"},
            },
        ),
        source="agent-orchestrator",
    )
    return env.model_copy(update={"occurred_at": NOW - timedelta(minutes=minutes_ago)})


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Container, object]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        Settings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        clock=Clock(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    handler = Inbox(container.tables.inbox, "approval-service.events").guard(
        container.sessions, on_event(container.record_request, container.record_outcome)
    )
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://approval"
    ) as client:
        yield client, container, handler
    await engine.dispose()


async def test_queue_decide_and_ticket_outcome(api) -> None:  # type: ignore[no-untyped-def]
    client, container, handler = api
    for env in (
        requested(1, minutes_ago=14),
        requested(2, severity="sev1", minutes_ago=3),
        requested(3, Gate.FIX),
    ):
        await handler(env)
    await handler(requested(1, minutes_ago=14))  # same approval again: ignored

    analyst = user(Role.L1_ANALYST)
    body = (await client.get("/v1/approvals", headers=analyst)).json()
    assert [i["id"] for i in body["items"]] == ["a-1", "a-2", "a-3"]  # oldest first
    assert body["stats"] == {
        "waiting": 3,
        "waiting_by_gate": {"ticket": 2, "fix": 1, "message": 0},
        "oldest_waiting_seconds": 14 * 60,
        "sev1_waiting": 1,
    }
    assert body["items"][2]["approver"] == "Support agent"
    tickets = (await client.get("/v1/approvals", params={"gate": "ticket"}, headers=analyst)).json()
    assert [i["id"] for i in tickets["items"]] == ["a-1", "a-2"]
    detail = (await client.get("/v1/approvals/a-1", headers=analyst)).json()
    assert detail["proposal"]["email"]["body"] == "Hello [PERSON_1]"

    r = await client.post("/v1/approvals/a-3/decision", headers=analyst, json={"decision": "approve"})
    assert r.status_code == 403  # T2 is for support agents only
    r = await client.post("/v1/approvals/a-1/decision", headers=analyst, json={"decision": "reject"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_decision"
    r = await client.post(
        "/v1/approvals/a-1/decision",
        headers=analyst,
        json={"decision": "approve", "edits": {"severity": "sev1"}},
    )
    assert (
        r.status_code == 200 and r.json()["status"] == "approved" and r.json()["outcome"] == "ticket_pending"
    )
    again = await client.post("/v1/approvals/a-1/decision", headers=analyst, json={"decision": "approve"})
    assert again.status_code == 200  # repeat: no second event
    other = await client.post(
        "/v1/approvals/a-1/decision",
        headers=user(Role.SUPPORT_AGENT, uid="u-2"),
        json={"decision": "approve"},
    )
    assert other.status_code == 409

    async with container.sessions() as s:
        events = [json.loads(b) for b in (await s.execute(select(container.tables.outbox.c.body))).scalars()]
    assert len(events) == 1 and events[0]["type"] == "approval.decided"
    assert (
        events[0]["payload"]["edits"] == {"severity": "sev1"}
        and events[0]["payload"]["decided_by_role"] == "l1_analyst"
    )

    await handler(
        EventEnvelope.wrap(
            JiraIssueCreated(approval_id="a-1", thread_id="t-1", issue_key="KAN-15", project="KAN"),
            source="jira-connector",
        )
    )
    done = (await client.get("/v1/approvals/a-1", headers=analyst)).json()
    assert done["outcome"] == "ticket_created" and done["jira_key"] == "KAN-15"
    decided = (await client.get("/v1/approvals", params={"decided": "true"}, headers=analyst)).json()
    assert [i["id"] for i in decided["items"]] == ["a-1"] and decided["stats"]["waiting"] == 2


async def test_queue_is_for_case_roles(api) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = api
    assert (await client.get("/v1/approvals")).status_code == 401
    assert (await client.get("/v1/approvals", headers=user(Role.ADMIN))).status_code == 403
    assert (await client.get("/v1/approvals/missing", headers=user(Role.SUPPORT_MANAGER))).status_code == 404
