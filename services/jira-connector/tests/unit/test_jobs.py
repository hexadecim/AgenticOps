from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from jira_connector.adapters.inbound.events import on_event
from jira_connector.bootstrap import Container, build_container
from jira_connector.config import Settings
from jira_connector.domain.model import JobStatus, adf
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import ApprovalDecided, ApprovalRequested, Decision, Gate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import NOW, FakeSource, FakeTracker, TrackerRejectedError, TrackerUnavailableError


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class World:
    def __init__(self, container: Container, tracker: FakeTracker, source: FakeSource, clock: Clock) -> None:
        self.c, self.tracker, self.source, self.clock = container, tracker, source, clock
        self.handler = Inbox(container.tables.inbox, "jira-connector.events").guard(
            container.sessions,
            on_event(container.store_proposal, container.queue_job, container.active, lambda: None),
        )

    async def propose_and_approve(
        self, n: int = 1, *, action: str = "create", edits: dict[str, Any] | None = None
    ) -> None:
        proposal = {
            "action": action,
            "ticket": {
                "summary": "[SITE-A014] Upload stuck at 0%",
                "description": "Upload stuck.\nThree retries.",
                "severity": "sev2",
                "site": "SITE-A014",
                "category": "upload failure",
            },
            "duplicate_of": {"key": "KAN-3", "score": 0.91} if action == "link" else None,
            "reporter_token": "PERSON_1",
        }
        await self.handler(
            EventEnvelope.wrap(
                ApprovalRequested(
                    approval_id=f"a-{n}",
                    run_id=f"r-{n}",
                    thread_id=f"t-{n}",
                    gate=Gate.TICKET,
                    title="Upload stuck",
                    detail="d",
                    proposal=proposal,
                ),
                source="agent-orchestrator",
            )
        )
        await self.handler(
            EventEnvelope.wrap(
                ApprovalDecided(
                    approval_id=f"a-{n}",
                    run_id=f"r-{n}",
                    thread_id=f"t-{n}",
                    gate=Gate.TICKET,
                    decision=Decision.APPROVE,
                    decided_by="u-1",
                    decided_by_role="l1_analyst",
                    edits=edits or {},
                ),
                source="approval-service",
            )
        )

    async def events(self) -> list[dict[str, Any]]:
        async with self.c.sessions() as s:
            return [json.loads(b) for b in (await s.execute(select(self.c.tables.outbox.c.body))).scalars()]

    async def job(self, n: int = 1) -> Any:
        async with self.c.sessions() as s:
            return (
                await s.execute(select(self.c.tables.jobs).where(self.c.tables.jobs.c.id == f"a-{n}"))
            ).first()


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tracker, source, clock = FakeTracker(), FakeSource(), Clock()
    container = build_container(
        Settings(jwt_secret="j" * 32, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        source=source,
        factory=lambda s: tracker,
        clock=clock,
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    yield World(container, tracker, source, clock)
    await engine.dispose()


async def test_approved_ticket_is_created_once_with_edits_and_labels(world: World) -> None:
    await world.propose_and_approve(edits={"severity": "sev1"})
    assert await world.c.process_jobs() == 1
    ((key, (fields, labels)),) = world.tracker.issues.items()
    assert key == "KAN-1" and fields.summary == "[SITE-A014] Upload stuck at 0%"
    assert (
        "reporter [PERSON_1]" in fields.description
        and "Personal data is replaced by tokens" in fields.description
    )
    assert labels == ["opspilot", "opspilot-a-1", "sev1", "site-a014", "upload-failure"]
    (event,) = await world.events()
    assert event["type"] == "jira.issue.created" and event["payload"]["issue_key"] == "KAN-1"
    assert event["payload"]["severity"] == "sev1" and event["payload"]["run_id"] == "r-1"
    assert await world.c.process_jobs() == 0  # done


async def test_replay_after_a_crash_finds_the_issue_instead_of_creating_another(world: World) -> None:
    await world.propose_and_approve()
    world.tracker.issues["KAN-7"] = (None, ["opspilot", "opspilot-a-1"])  # type: ignore[assignment]  # created, then crashed
    await world.c.process_jobs()
    assert list(world.tracker.issues) == ["KAN-7"]
    assert (await world.events())[0]["payload"]["issue_key"] == "KAN-7"


async def test_jira_outage_retries_later_without_losing_the_job(world: World) -> None:
    await world.propose_and_approve()
    world.tracker.fail = TrackerUnavailableError("Jira kept failing", retry_after=60)
    await world.c.process_jobs()
    job = await world.job()
    assert job.status == JobStatus.PENDING.value and job.attempts == 1 and "kept failing" in job.last_error
    assert await world.c.process_jobs() == 0  # not due yet
    world.tracker.fail, world.clock.at = None, NOW + timedelta(seconds=61)
    assert await world.c.process_jobs() == 1
    assert (await world.job()).issue_key == "KAN-1"


async def test_permanent_refusal_is_reported(world: World) -> None:
    await world.propose_and_approve()
    world.tracker.fail = TrackerRejectedError("Jira refused the request: issuetype: Incident is not valid")
    await world.c.process_jobs()
    assert (await world.job()).status == JobStatus.FAILED.value
    (event,) = await world.events()
    assert event["type"] == "jira.issue.failed" and "issuetype" in event["payload"]["reason"]


async def test_waits_while_jira_is_not_configured(world: World) -> None:
    world.source.settings = None
    await world.propose_and_approve()
    await world.c.process_jobs()
    job = await world.job()
    assert job.status == "pending" and job.attempts == 0 and "not configured" in job.last_error


async def test_duplicate_is_linked_by_comment(world: World) -> None:
    await world.propose_and_approve(action="link")
    await world.c.process_jobs()
    assert world.tracker.issues == {}
    ((key, text),) = world.tracker.comments
    assert key == "KAN-3" and "[PERSON_1] at SITE-A014" in text
    (event,) = await world.events()
    assert event["type"] == "jira.issue.linked" and event["payload"]["approval_id"] == "a-1"


async def test_rejected_or_other_gates_never_reach_jira(world: World) -> None:
    await world.handler(
        EventEnvelope.wrap(
            ApprovalDecided(
                approval_id="a-9",
                run_id="r",
                thread_id="t",
                gate=Gate.TICKET,
                decision=Decision.REJECT,
                decided_by="u",
                decided_by_role="l1_analyst",
                reason="spam",
            ),
            source="approval-service",
        )
    )
    assert await world.c.process_jobs() == 0 and await world.events() == []


async def test_context_for_dedupe(world: World) -> None:
    await world.propose_and_approve()
    await world.c.process_jobs()
    ctx = await world.c.get_context(24)
    assert (ctx.configured, ctx.project, ctx.issue_type) == (True, "KAN", "Incident")
    assert [i.key for i in ctx.recent] == ["KAN-1"]
    world.source.settings = None
    assert (await world.c.active.get(refresh=True)) is None and world.tracker.closed
    assert (await world.c.get_context(24)).configured is False


def test_adf_paragraphs_and_line_breaks() -> None:
    doc = adf("First line\nsecond line\n\nNew paragraph")
    assert doc["type"] == "doc" and len(doc["content"]) == 2
    assert [n["type"] for n in doc["content"][0]["content"]] == ["text", "hardBreak", "text"]
