"""Diagnostics → T2 → executed fix → health re-check, with the real graph and runner and fake ports."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
from agent_orchestrator.adapters.inbound.events import on_event
from agent_orchestrator.adapters.outbound.sql import SqlEventSink, SqlThreadTickets
from agent_orchestrator.bootstrap import Container, build_container
from agent_orchestrator.config import Settings
from agent_orchestrator.domain.diagnosis import FixChoice, choice_schema, render_evidence
from agent_orchestrator.domain.errors import DiagnosisUnusableError, OpsUnavailableError
from agent_orchestrator.domain.run import RunStatus
from agent_orchestrator.domain.triage import approval_id_for
from agent_orchestrator.ports import AgentPorts
from langgraph.checkpoint.memory import InMemorySaver
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import (
    ActionExecuted,
    ApprovalDecided,
    Decision,
    EmailReceived,
    Gate,
    JiraIssueCreated,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from .test_workflow import NOW, SUPPORT, Circuit, Clock, FakeEmbedder, FakeIssues, FakeModel, FakePrivacy

CANDIDATE = {
    "action": "restart_service",
    "title": "Restart upload-worker on eu-west-1?",
    "signature": 'restart_service(service="upload-worker", region="eu-west-1")',
    "runbook": {"id": "RB-07", "title": "Upload storage errors", "step": 3, "why": "stale storage handle"},
    "params": {"service": "upload-worker", "region": "eu-west-1"},
    "params_spec": [
        {"name": "service", "label": "Service", "choices": ["upload-worker", "upload-api"]},
        {"name": "region", "label": "Region", "choices": ["eu-west-1"]},
    ],
    "effect": "restarts", "rollback": "none",
}  # fmt: skip
FAILING = {"name": "Upload", "ok": False, "detail": "timeout after 30 s, 0 bytes stored"}
PASSING = {"name": "Upload", "ok": True, "detail": "test file stored in 1.4 s"}


class FakeOps:
    def __init__(self) -> None:
        self.fail = False
        self.healthy_after = True
        self.candidates: list[dict[str, Any]] = [CANDIDATE]
        self.queries: list[dict[str, Any]] = []

    async def diagnose(self, **query: Any) -> dict[str, Any]:
        if self.fail:
            raise OpsUnavailableError("circuit open")
        self.queries.append(query)
        return {
            "region": "eu-west-1", "site": query["site"], "checked_at": NOW.isoformat(),
            "probes": [FAILING, {"name": "Portal", "ok": True, "detail": "200 in 180 ms"}],
            "logs": [{"service": "upload-worker", "lines": ["10:12:40 ERROR EIO (user=***)"], "similar": 4,
                      "since": "09:58:40"}],
            "runbooks": [{"id": "RB-07", "title": "Upload storage errors", "heading": "Step 3",
                          "text": "Restart", "score": 0.81, "method": "semantic"}],
            "candidates": self.candidates, "deployment": "upload-api v2.14.1, 3 days ago",
            "impact": {"sites": ["SITE-A014", "SITE-B207"], "uploads waiting": 41}, "notes": [],
            "suspects": ["upload-worker", "upload-api"],
        }  # fmt: skip

    async def probes(self, region: str | None, **_: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise OpsUnavailableError("circuit open")
        return [PASSING if self.healthy_after else FAILING]


class FakeAdvisor:
    def __init__(self) -> None:
        self.answer = FixChoice(
            likely_cause="upload-worker holds a stale storage handle", choice="1", confidence="high",
            explanation="Upload probe fails with EIO in the worker log; RB-07 step 3",
        )  # fmt: skip
        self.fail: Exception | None = None
        self.prompts: list[str] = []

    async def choose(self, evidence: str, candidates: int) -> FixChoice:
        self.prompts.append(evidence)
        if self.fail:
            raise self.fail
        return self.answer


class World:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.advance, _ = c.runner(InMemorySaver())
        self.handler = Inbox(c.tables.inbox, "agent-orchestrator.events").guard(
            c.sessions,
            on_event(
                c.start_run, c.record_decision, c.record_ticket, c.llm_settings, lambda: None, c.record_fix
            ),
        )

    @property
    def ops(self) -> FakeOps:
        return self.c.ports.ops  # type: ignore[return-value]

    @property
    def advisor(self) -> FakeAdvisor:
        return self.c.ports.advisor  # type: ignore[return-value]

    async def run(self) -> Any:
        async with self.c.uow() as uow:
            return await uow.runs.by_email("e-1")

    async def events(self, kind: str) -> list[dict[str, Any]]:
        async with self.c.sessions() as s:
            rows = [json.loads(b) for b in (await s.execute(select(self.c.tables.outbox.c.body))).scalars()]
        return [r["payload"] for r in rows if r["type"] == kind]

    async def send(self, payload: Any, source: str) -> None:
        await self.handler(EventEnvelope.wrap(payload, source=source))

    async def decide(self, gate: Gate, decision: Decision = Decision.APPROVE, **kw: Any) -> None:
        run = await self.run()
        await self.send(
            ApprovalDecided(
                approval_id=approval_id_for(run.id, gate.value), run_id=run.id, thread_id="t-1", gate=gate,
                decision=decision, decided_by="u-1", **{"decided_by_role": "support_agent", **kw},
            ),
            "approval-service",
        )  # fmt: skip

    async def ticket(self) -> None:
        run = await self.run()
        await self.send(
            JiraIssueCreated(
                approval_id=approval_id_for(run.id, "ticket"), thread_id="t-1", issue_key="KAN-12",
                project="KAN", run_id=run.id,
            ),
            "jira-connector",
        )  # fmt: skip

    async def executed(self, ok: bool = True) -> None:
        run = await self.run()
        await self.send(
            ActionExecuted(
                approval_id=approval_id_for(run.id, "fix"), action="restart_service",
                params=CANDIDATE["params"], ok=ok, detail="Mock executor restarted upload-worker" if ok else
                "Refused before running", run_id=run.id, executor="mock",
            ),
            "ops-service",
        )  # fmt: skip

    async def to_fix_gate(self) -> Any:
        await self.send(
            EmailReceived(email_id="e-1", thread_id="t-1", provider="imap", received_at=NOW), "mail-service"
        )
        await self.advance()
        await self.decide(Gate.TICKET, decided_by_role="l1_analyst")
        await self.advance()
        assert (await self.run()).status is RunStatus.APPROVED  # waiting for Jira at await_ticket
        await self.ticket()
        await self.advance()
        return await self.run()


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        Settings(jwt_secret="o" * 32, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        ports=AgentPorts(FakePrivacy(), FakeModel(SUPPORT), FakeEmbedder(), FakeIssues(), None, None),  # type: ignore[arg-type]
        llm_circuit=Circuit(),
        clock=Clock(),
    )
    p = container.ports
    container.ports = AgentPorts(
        p.privacy, p.intake, p.embedder, p.issues, SqlEventSink(container.sessions, container.tables),
        SqlThreadTickets(container.sessions, container.tables), FakeOps(), FakeAdvisor(),
    )  # fmt: skip
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    yield World(container)
    await engine.dispose()


async def test_ticket_then_diagnostics_then_fix_gate_then_health_check(world: World) -> None:
    run = await world.to_fix_gate()
    assert run.status is RunStatus.WAITING_APPROVAL and run.node == "gate_fix"
    assert run.approval_id == approval_id_for(run.id, "fix") and run.jira_key == "KAN-12"
    assert (
        world.ops.queries[0]["site"] == "SITE-A014" and "Upload stuck at 0%" in world.ops.queries[0]["text"]
    )
    assert "1. restart_service(" in world.advisor.prompts[0] and "user=***" in world.advisor.prompts[0]

    requests = await world.events("approval.requested")
    fix = next(r for r in requests if r["gate"] == "fix")
    assert fix["title"] == "Restart upload-worker on eu-west-1?" and fix["jira_key"] == "KAN-12"
    assert fix["detail"] == "Diagnostics: upload-worker holds a stale storage handle · runbook RB-07 step 3"
    p = fix["proposal"]
    assert p["action"]["params"] == {"service": "upload-worker", "region": "eu-west-1"}
    assert p["affected"] == {
        "sites": ["SITE-A014", "SITE-B207"],
        "reporters": 1,
        "impact": {"uploads waiting": 41},
    }
    assert p["evidence"]["probes"][0] == FAILING and p["services"] == ["upload-worker", "upload-api"]
    assert p["diagnosis"]["confidence"] == "high" and p["region"] == "eu-west-1"

    # Support agent approves; ops-service's result arrives before the worker even parks at await_fix.
    await world.decide(Gate.FIX, edits={"params": {"service": "upload-worker"}})
    await world.executed()
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.RESOLVED and run.detail == "All 1 probes pass"
    tools = [(e["agent"], e["tool"]) for e in await world.events("agent.tool_called")]
    assert tools[-3:] == [
        ("Diagnostics", "gather_evidence"),
        ("Diagnostics", "propose_fix"),
        ("Health", "recheck_health"),
    ]
    statuses = [e["status"] for e in await world.events("run.status_changed")]
    assert statuses[-3:] == ["ticket_created", "waiting_approval", "resolved"]
    assert "resuming" not in statuses


async def test_fix_waits_for_ops_result_then_fails_when_probes_still_fail(world: World) -> None:
    await world.to_fix_gate()
    await world.decide(Gate.FIX)
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.FIX_APPROVED and run.node == "await_fix"
    assert run.detail == "Fix approved; ops-service is running restart_service"
    world.ops.healthy_after = False
    await world.executed()
    await world.executed()  # repeat: ignored
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.FIX_FAILED
    assert run.detail.startswith("Still failing: Upload (timeout after 30 s") and "on-call" in run.detail


async def test_refused_fix_fails_and_rejected_fix_ends(world: World) -> None:
    await world.to_fix_gate()
    await world.decide(Gate.FIX)
    await world.advance()
    await world.executed(ok=False)
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.FIX_FAILED and "did not run: Refused before running" in run.detail


async def test_rejected_fix_ends_the_run_with_the_reason(world: World) -> None:
    await world.to_fix_gate()
    await world.decide(Gate.FIX, Decision.REJECT, reason="Engineering is already on it")
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.FIX_REJECTED and run.detail == "Engineering is already on it"


async def test_no_candidate_or_model_says_none_ends_at_the_ticket(world: World) -> None:
    world.ops.candidates = []
    run = await world.to_fix_gate()
    assert run.status is RunStatus.TICKET_CREATED
    assert run.detail == "No allow-listed fix in the runbooks fits this problem"
    assert world.advisor.prompts == []  # the model is not asked when there is nothing to choose


async def test_model_choosing_none_or_unreadable_answer_proposes_nothing(world: World) -> None:
    world.advisor.answer = world.advisor.answer.model_copy(
        update={"choice": "none", "likely_cause": "site proxy"}
    )
    run = await world.to_fix_gate()
    assert run.status is RunStatus.TICKET_CREATED
    assert run.detail == "The evidence supports none of the allow-listed fixes: site proxy"
    assert not [r for r in await world.events("approval.requested") if r["gate"] == "fix"]


async def test_unreadable_model_answer_proposes_nothing(world: World) -> None:
    world.advisor.fail = DiagnosisUnusableError("the model's answer could not be read")
    run = await world.to_fix_gate()
    assert run.status is RunStatus.TICKET_CREATED and run.detail.startswith("No fix proposed:")


async def test_ops_service_down_parks_the_run_until_evidence_is_back(world: World) -> None:
    world.ops.fail = True
    run = await world.to_fix_gate()
    assert run.status is RunStatus.WAITING_OPS and run.detail == "Evidence unavailable: circuit open"
    world.ops.fail = False
    world.c.clock.at = NOW.replace(minute=5)  # type: ignore[attr-defined]
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.WAITING_APPROVAL and run.node == "gate_fix"


def test_choice_schema_only_allows_offered_numbers_or_none() -> None:
    schema = choice_schema(2)
    assert schema.model_json_schema()["properties"]["choice"]["enum"] == ["1", "2", "none"]
    schema.model_validate({"likely_cause": "x", "choice": "2", "confidence": "low", "explanation": "y"})
    with pytest.raises(ValidationError):
        schema.model_validate({"likely_cause": "x", "choice": "3", "confidence": "low", "explanation": "y"})


def test_rendered_evidence_numbers_the_candidates() -> None:
    text = render_evidence(SUPPORT, "KAN-12", {"probes": [FAILING], "candidates": [CANDIDATE], "logs": []})
    assert "Ticket KAN-12: Upload stuck at 0%" in text and "- Upload: FAIL, timeout" in text
    assert '1. restart_service(service="upload-worker", region="eu-west-1"): runbook RB-07 step 3' in text
