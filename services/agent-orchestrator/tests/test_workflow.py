"""The real graph and runner with fake ports, an in-memory checkpointer and SQL runs (SQLite)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from agent_orchestrator.adapters.inbound.events import on_event
from agent_orchestrator.adapters.outbound.sql import SqlEventSink, SqlThreadTickets
from agent_orchestrator.bootstrap import Container, build_container
from agent_orchestrator.config import Settings
from agent_orchestrator.domain.errors import (
    ContextUnavailableError,
    LlmUnavailableError,
    PrivacyUnavailableError,
    StopRunError,
)
from agent_orchestrator.domain.run import RunStatus
from agent_orchestrator.domain.triage import Intake, approval_id_for
from agent_orchestrator.main import create_app
from agent_orchestrator.ports import AgentPorts
from langgraph.checkpoint.memory import InMemorySaver
from opspilot_common.auth import Principal, Role, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import ApprovalDecided, Decision, EmailReceived, Gate, JiraIssueCreated
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
EMAIL = {
    "email_id": "e-1",
    "thread_id": "t-1",
    "from": {"email_token": "EMAIL_1", "name_token": "PERSON_1"},
    "subject": "Upload stuck at 0%, telemetry_0925.zip",
    "body": "Hello support,\nSince 08:40 our upload is stuck at 0% on M-2210.\nRegards,\n[signature removed]",
    "received_at": "2026-10-08T08:52:00+00:00",
    "attachments": [],
}
SUPPORT = Intake(
    is_support_request=True,
    category="upload failure",
    severity="sev2",
    summary="Upload stuck at 0%",
    site="SITE-A014",
    asset="M-2210",
    error="stuck at 0%",
    missing_details=[],
)


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class FakePrivacy:
    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.calls = 0

    async def pseudonymise(self, email_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise self.fail
        return {**EMAIL, "email_id": email_id}, {"record_id": "vlt-7Q1", "summary": "PERSON (1) tokenised"}


class FakeModel:
    def __init__(self, answer: Intake = SUPPORT) -> None:
        self.answer, self.calls = answer, 0
        self.fail: Exception | None = None

    async def classify(self, email: dict[str, Any]) -> Intake:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.answer

    async def describe(self) -> dict[str, Any]:
        return {"model": "deepseek-r1:8b", "thinking": True}


class FakeEmbedder:
    """Vectors chosen so 'Upload stuck' matches KAN-3 closely and the rest far apart."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "Upload" in t else [0.0, 1.0] for t in texts]


class FakeIssues:
    def __init__(self) -> None:
        self.recent: list[dict[str, Any]] = [{"key": "KAN-9", "summary": "Printer jam", "status": "To Do"}]
        self.fail = False

    async def context(self, hours: int) -> dict[str, Any]:
        if self.fail:
            raise ContextUnavailableError("jira-connector down")
        return {"configured": True, "project": "KAN", "issue_type": "Incident", "recent": self.recent}


class Circuit:
    def __init__(self) -> None:
        self.wait: float | None = None

    async def retry_after(self) -> float | None:
        return self.wait


class World:
    def __init__(self, container: Container, clock: Clock, circuit: Circuit, saver: InMemorySaver) -> None:
        self.c, self.clock, self.circuit = container, clock, circuit
        self.ports: AgentPorts = container.ports
        self.advance, self.worker = container.runner(saver)
        self.handler = Inbox(container.tables.inbox, "agent-orchestrator.events").guard(
            container.sessions,
            on_event(
                container.start_run,
                container.record_decision,
                container.record_ticket,
                container.llm_settings,
                lambda: None,
            ),
        )

    @property
    def privacy(self) -> FakePrivacy:
        return self.ports.privacy  # type: ignore[return-value]

    @property
    def model(self) -> FakeModel:
        return self.ports.intake  # type: ignore[return-value]

    @property
    def issues(self) -> FakeIssues:
        return self.ports.issues  # type: ignore[return-value]

    async def email(self, n: int = 1, thread: str = "t-1") -> None:
        await self.handler(
            EventEnvelope.wrap(
                EmailReceived(email_id=f"e-{n}", thread_id=thread, provider="imap", received_at=NOW),
                source="mail-service",
            )
        )

    async def run(self, n: int = 1) -> Any:
        async with self.c.uow() as uow:
            return await uow.runs.by_email(f"e-{n}")

    async def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        async with self.c.sessions() as s:
            rows = [json.loads(b) for b in (await s.execute(select(self.c.tables.outbox.c.body))).scalars()]
        return [r for r in rows if kind is None or r["type"] == kind]

    async def decide(self, run: Any, decision: Decision, reason: str | None = None) -> None:
        await self.handler(
            EventEnvelope.wrap(
                ApprovalDecided(
                    approval_id=run.approval_id,
                    run_id=run.id,
                    thread_id=run.thread_id,
                    gate=Gate.TICKET,
                    decision=decision,
                    decided_by="u-1",
                    decided_by_role="l1_analyst",
                    reason=reason,
                ),
                source="approval-service",
            )
        )


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    clock, circuit = Clock(), Circuit()
    container = build_container(
        Settings(jwt_secret="o" * 32, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        ports=AgentPorts(FakePrivacy(), FakeModel(), FakeEmbedder(), FakeIssues(), None, None),  # type: ignore[arg-type]
        llm_circuit=circuit,
        clock=clock,
    )
    container.ports = AgentPorts(
        container.ports.privacy,
        container.ports.intake,
        container.ports.embedder,
        container.ports.issues,
        SqlEventSink(container.sessions, container.tables),
        SqlThreadTickets(container.sessions, container.tables),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    yield World(container, clock, circuit, InMemorySaver())
    await engine.dispose()


async def test_email_to_ticket_gate_and_approval(world: World) -> None:
    await world.email()
    await world.email()  # redelivered: one run only
    assert await world.advance() == 1
    run = await world.run()
    assert run.status is RunStatus.WAITING_APPROVAL and run.approval_id == approval_id_for(run.id, "ticket")

    (requested,) = await world.events("approval.requested")
    p = requested["payload"]
    assert p["gate"] == "ticket" and p["title"] == "Upload stuck at 0%, telemetry_0925.zip"
    assert p["detail"] == "Intake: upload failure · no related open ticket" and p["site"] == "SITE-A014"
    proposal = p["proposal"]
    assert (
        proposal["action"] == "create" and proposal["ticket"]["summary"] == "[SITE-A014] Upload stuck at 0%"
    )
    assert proposal["reporter_token"] == "PERSON_1" and proposal["privacy"]["record_id"] == "vlt-7Q1"
    assert proposal["dedupe"]["searched"] == 1 and proposal["dedupe"]["best"]["key"] == "KAN-9"
    assert proposal["jira"] == {"configured": True, "project": "KAN", "issue_type": "Incident"}
    tools = [e["payload"]["tool"] for e in await world.events("agent.tool_called")]
    assert tools == ["classify", "search_jira"]

    await world.decide(run, Decision.APPROVE)
    await world.decide(run, Decision.APPROVE)  # duplicate decision event: ignored
    assert await world.advance() == 1
    run = await world.run()
    assert run.status is RunStatus.APPROVED
    assert len(await world.events("approval.requested")) == 1  # not re-sent on resume
    assert world.model.calls == 1 and world.privacy.calls == 1  # earlier nodes did not run again

    await world.handler(
        EventEnvelope.wrap(
            JiraIssueCreated(
                approval_id=run.approval_id, thread_id="t-1", issue_key="KAN-15", project="KAN", run_id=run.id
            ),
            source="jira-connector",
        )
    )
    run = await world.run()
    assert run.status is RunStatus.TICKET_CREATED and run.jira_key == "KAN-15"


async def test_rejection_ends_the_run(world: World) -> None:
    await world.email()
    await world.advance()
    await world.decide(await world.run(), Decision.REJECT, "Duplicate of a call")
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.REJECTED and run.detail == "Duplicate of a call"


async def test_not_a_support_request_is_ignored_without_approval(world: World) -> None:
    world.model.answer = SUPPORT.model_copy(update={"is_support_request": False, "category": "newsletter"})
    await world.email()
    await world.advance()
    assert (await world.run()).status is RunStatus.IGNORED
    assert await world.events("approval.requested") == []


async def test_llm_outage_parks_the_run_and_it_resumes_later(world: World) -> None:
    world.model.fail = LlmUnavailableError("Ollama unreachable", retry_after=90)
    await world.email()
    await world.advance()
    run = await world.run()
    assert (
        run.status is RunStatus.WAITING_LLM
        and "AI paused" in run.detail
        and run.due_at == NOW + timedelta(seconds=90)
    )

    world.clock.at = NOW + timedelta(seconds=91)
    world.circuit.wait = 30  # circuit still open: do not even try
    await world.advance()
    assert world.model.calls == 1 and (await world.run()).status is RunStatus.WAITING_LLM

    world.clock.at, world.circuit.wait, world.model.fail = NOW + timedelta(seconds=200), None, None
    await world.advance()
    assert (await world.run()).status is RunStatus.WAITING_APPROVAL
    assert world.privacy.calls == 1  # the privacy step was not repeated


async def test_privacy_outage_fails_closed_and_waits(world: World) -> None:
    world.privacy.fail = PrivacyUnavailableError("privacy-service is unavailable")
    await world.email()
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.WAITING_PRIVACY and world.model.calls == 0  # nothing reached the model


async def test_stop_and_repeated_errors_fail_the_run(world: World) -> None:
    world.privacy.fail = StopRunError("The email no longer exists")
    await world.email()
    await world.advance()
    assert (await world.run()).status is RunStatus.FAILED

    world.privacy.fail = None
    world.model.fail = ValueError("model answered nonsense")
    await world.email(2, thread="t-2")
    for minutes in (0, 2, 5):
        world.clock.at = NOW + timedelta(minutes=minutes)
        await world.advance()
    run = await world.run(2)
    assert run.status is RunStatus.FAILED and run.attempts == 3 and "nonsense" in run.detail


async def test_similar_open_incident_or_same_thread_proposes_a_link(world: World) -> None:
    world.issues.recent = [{"key": "KAN-3", "summary": "[SITE-A014] Upload broken", "status": "In Progress"}]
    await world.email()
    await world.advance()
    p = (await world.events("approval.requested"))[0]["payload"]
    assert p["proposal"]["action"] == "link" and p["proposal"]["duplicate_of"]["key"] == "KAN-3"
    assert p["detail"].endswith("possible duplicate of KAN-3 (1.00)") and p["jira_key"] == "KAN-3"

    run = await world.run()
    await world.decide(run, Decision.APPROVE)
    await world.advance()
    await world.handler(
        EventEnvelope.wrap(
            JiraIssueCreated(
                approval_id=run.approval_id, thread_id="t-1", issue_key="KAN-3", project="KAN", run_id=run.id
            ),
            source="jira-connector",
        )
    )
    world.issues.recent = []
    await world.email(2, thread="t-1")  # a reply in the same mail thread
    await world.advance()
    second = (await world.events("approval.requested"))[1]["payload"]["proposal"]
    assert second["duplicate_of"] == {"key": "KAN-3", "score": 1.0, "reason": "same email thread"}


async def test_jira_search_down_still_reaches_the_gate(world: World) -> None:
    world.issues.fail = True
    await world.email()
    await world.advance()
    assert (await world.run()).status is RunStatus.WAITING_APPROVAL
    p = (await world.events("approval.requested"))[0]["payload"]
    assert p["detail"].endswith("duplicates not checked") and "unavailable" in p["proposal"]["dedupe"]["note"]


async def test_summary_api_reports_ai_paused(world: World) -> None:
    world.model.fail = LlmUnavailableError("Ollama unreachable")
    await world.email()
    await world.advance()
    app = create_app(world.c, with_events=False, checkpointer=InMemorySaver())
    token = TokenIssuer("o" * 32).issue(Principal("u", "L", frozenset({Role.L1_ANALYST})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://o") as client:
        r = await client.get("/v1/runs/summary", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ai_paused"] is True and r.json()["counts"]["waiting_llm"] == 1


def test_model_quirks_are_normalised() -> None:
    raw = {
        "is_support_request": True,
        "category": "VPN",
        "severity": "sev1",
        "site": "None",
        "asset": " n/a ",
        "error": "809",
        "missing_details": ["", "None", "machine id"],
        "summary": "VPN connection issue at location SITE-B207 in Hamburg with error code 809, "
        "affecting user [PERSON_2] from accounting. Please call [PHONE_1] for assistance.",
    }
    intake = Intake.model_validate(raw)
    assert intake.site is None and intake.asset is None and intake.error == "809"
    assert intake.missing_details == ["machine id"]
    assert len(intake.summary) <= 100 and intake.summary.endswith("…") and " " not in intake.summary[-2:]


def test_ids_are_kept_without_trailing_prose() -> None:
    base = {"is_support_request": True, "category": "Upload", "severity": "sev2", "summary": "Upload stuck"}
    intake = Intake.model_validate(
        {**base, "site": "SITE-A014 (machine M-221", "asset": "M-2210, the scanner"}
    )
    assert (intake.site, intake.asset) == ("SITE-A014", "M-2210")
    assert Intake.model_validate({**base, "site": "Hamburg"}).site == "Hamburg"
    assert Intake.model_validate({**base, "asset": "upload service"}).asset == "upload service"


def test_phrases_that_say_nothing_are_empty() -> None:
    base = {"is_support_request": True, "category": "IT", "severity": "sev3", "summary": "Printer"}
    for phrase in ("None reported in email,", "Not mentioned", "n/a (no site)", "No site given", "unknown"):
        assert Intake.model_validate({**base, "site": phrase}).site is None
    assert Intake.model_validate({**base, "site": "Nordhafen"}).site == "Nordhafen"  # a name, not "no"


async def test_unusable_answer_is_retried_without_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_orchestrator.adapters.outbound.llm import LlmSettings, OllamaIntake
    from langchain_core.exceptions import OutputParserException
    from opspilot_common.resilience import ResilienceRegistry

    class FixedSettings:
        async def get(self, refresh: bool = False) -> LlmSettings:
            return LlmSettings(1, "http://ollama.test", "deepseek-r1:8b", "nomic-embed-text", True)

    intake, calls = OllamaIntake(FixedSettings(), ResilienceRegistry().get("ollama")), []  # type: ignore[arg-type]

    async def ask(cfg: Any, email: Any, *, thinking: bool) -> Intake:
        calls.append(thinking)
        if thinking:
            raise OutputParserException("Invalid json output: ")
        return SUPPORT

    monkeypatch.setattr(intake, "_ask", ask)
    assert await intake.classify(EMAIL) == SUPPORT and calls == [True, False]
