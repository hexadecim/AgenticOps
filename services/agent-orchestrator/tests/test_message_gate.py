"""Comms → T3 → send, with the real graph and runner and fake ports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
from agent_orchestrator.adapters.inbound.events import on_event
from agent_orchestrator.adapters.outbound.sql import SqlEventSink, SqlThreadTickets
from agent_orchestrator.bootstrap import build_container
from agent_orchestrator.config import Settings
from agent_orchestrator.domain.comms import clean_model_text, fallback_text, kind_for, subject_for
from agent_orchestrator.domain.errors import DraftUnusableError, LlmUnavailableError
from agent_orchestrator.domain.run import RunStatus
from agent_orchestrator.domain.triage import approval_id_for
from agent_orchestrator.ports import AgentPorts
from langgraph.checkpoint.memory import InMemorySaver
from opspilot_common.drafts import AI_DISCLOSURE, RECIPIENT_TOKEN
from opspilot_common.events import Inbox
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    Decision,
    EmailReceived,
    Gate,
    JiraIssueCreated,
    JiraIssueLinked,
    MessageFailed,
    MessageSent,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from .test_fix_gate import NOW, FakeAdvisor, FakeOps, World
from .test_workflow import SUPPORT, Circuit, Clock, FakeEmbedder, FakeIssues, FakeModel, FakePrivacy

GOOD = (
    "Hello [PERSON_1],\n\nThe upload problem has been fixed since the restart.\n\n"
    "Kind regards,\nOpsPilot Support"
)


NEEDS_DETAILS = SUPPORT.model_copy(update={"missing_details": ["Upload file name", "Browser version 12"]})


class FakeComms:
    def __init__(self) -> None:
        self.answer = GOOD
        self.fail: Exception | None = None
        self.briefs: list[str] = []

    async def write(self, brief: str) -> str:
        self.briefs.append(brief)
        if self.fail:
            raise self.fail
        return self.answer


class Messages(World):
    def __init__(self, c: Any) -> None:
        super().__init__(c)
        self.handler = Inbox(c.tables.inbox, "agent-orchestrator.events").guard(
            c.sessions,
            on_event(
                c.start_run, c.record_decision, c.record_ticket, c.llm_settings, lambda: None, c.record_fix,
                c.record_message,
            ),
        )  # fmt: skip

    @property
    def comms(self) -> FakeComms:
        return self.c.ports.comms  # type: ignore[return-value]

    @property
    def model(self) -> FakeModel:
        return self.c.ports.intake  # type: ignore[return-value]

    async def messages(self) -> list[dict[str, Any]]:
        return [r for r in await self.events("approval.requested") if r["gate"] == "message"]

    async def to_message_gate(self) -> Any:
        """Ticket, fix and a passing health re-check: the run reaches T3."""
        await self.to_fix_gate()
        await self.decide(Gate.FIX)
        await self.executed()
        await self.advance()
        return await self.run()

    async def decide_message(self, decision: Decision = Decision.APPROVE, **kw: Any) -> None:
        run = await self.run()
        await self.send(
            ApprovalDecided(
                approval_id=run.approval_id, run_id=run.id, thread_id="t-1", gate=Gate.MESSAGE,
                decision=decision, decided_by="u-1", decided_by_role="l1_analyst", **kw,
            ),
            "approval-service",
        )  # fmt: skip
        await self.advance()

    async def sent(self, *failed: str) -> None:
        run = await self.run()
        await self.send(
            MessageSent(
                message_id="m-1", thread_id="t-1", recipient_tokens=["PERSON_1"], jira_key=run.jira_key,
                run_id=run.id, approval_id=run.approval_id, failed_tokens=list(failed),
                detail="refused" if failed else None,
            ),
            "mail-service",
        )  # fmt: skip
        await self.advance()


@pytest.fixture
async def world() -> AsyncIterator[Messages]:
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
        SqlThreadTickets(container.sessions, container.tables), FakeOps(), FakeAdvisor(), FakeComms(),
    )  # fmt: skip
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    yield Messages(container)
    await engine.dispose()


async def test_resolved_fix_reaches_t3_then_sends_after_approval(world: Messages) -> None:
    run = await world.to_message_gate()
    assert run.status is RunStatus.WAITING_APPROVAL and run.node == "gate_message"
    assert run.approval_id == approval_id_for(run.id, "message:0")

    # The verdict was announced at once: reporting and T2's screen need not wait for a person at T3.
    verdicts = [e for e in await world.events("run.status_changed") if e["status"] == "resolved"]
    assert len(verdicts) == 1 and verdicts[0]["node"] == "health_check"

    (request,) = await world.messages()
    assert request["title"] == "Resolution update for KAN-12" and request["jira_key"] == "KAN-12"
    assert request["detail"] == "Comms: Resolution update to 1 reporter · all checks pass"
    p = request["proposal"]
    assert p["kind"] == "resolution" and p["written_by"] == "model" and p["revision"] == 0
    assert p["subject"] == "Re: Upload stuck at 0%, telemetry_0925.zip [KAN-12]"
    assert p["body"].startswith(f"Hello {RECIPIENT_TOKEN},\n\nThe upload problem has been fixed")
    assert "[PERSON_1]" not in p["body"] and "Kind regards" not in p["body"]
    assert "Reference: KAN-12\n\nOpsPilot Support\n\n" + AI_DISCLOSURE in p["body"]
    assert all(c["ok"] for c in p["checks"])
    assert p["recipients"] == [
        {"email_token": "EMAIL_1", "name_token": "PERSON_1", "email_id": "e-1", "site": "SITE-A014",
         "how": "Reported"},
    ]  # fmt: skip
    assert not await world.events("message.send.requested")  # nothing leaves before the decision

    brief = world.comms.briefs[0]
    assert "the problem is fixed" in brief and "Ticket: KAN-12" in brief and "uploads waiting: 41" in brief
    assert "upload-worker" not in brief  # no internal service names or log lines for the model to repeat
    assert "restarts" not in brief  # the runbook's expected effect is not a fact about what happened
    assert "What was done: our team applied a fix" in brief

    await world.decide_message()
    run = await world.run()
    assert run.status is RunStatus.MESSAGE_APPROVED and run.node == "await_send"
    (send,) = await world.events("message.send.requested")
    assert send["approval_id"] == run.approval_id and send["recipient_tokens"] == ["PERSON_1"]
    assert send["recipients"] == [{"email_token": "EMAIL_1", "name_token": "PERSON_1", "email_id": "e-1"}]
    assert send["kind"] == "resolution" and send["body"] == p["body"]

    await world.sent()
    run = await world.run()
    assert run.status is RunStatus.RESOLVED and run.detail == "All 1 probes pass. Customer message sent"


async def test_the_approvers_edits_replace_the_draft(world: Messages) -> None:
    await world.to_message_gate()
    edits = {"body": "Edited body", "subject": "Edited subject"}
    await world.decide_message(edits=edits)
    (send,) = await world.events("message.send.requested")
    assert send["body"] == "Edited body" and send["subject"] == "Edited subject"


async def test_send_back_redrafts_with_the_note_up_to_two_times(world: Messages) -> None:
    await world.to_message_gate()
    await world.decide_message(Decision.SEND_BACK, reason="Mention the 41 retried uploads")
    run = await world.run()
    assert run.status is RunStatus.WAITING_APPROVAL and run.approval_id == approval_id_for(
        run.id, "message:1"
    )
    assert "Mention the 41 retried uploads" in world.comms.briefs[1]
    second = (await world.messages())[1]["proposal"]
    assert second["revision"] == 1 and second["notes"] == ["Mention the 41 retried uploads"]

    await world.decide_message(Decision.SEND_BACK, reason="Shorter")
    third = (await world.messages())[2]["proposal"]
    assert third["revision"] == 2 and third["notes"] == ["Mention the 41 retried uploads", "Shorter"]

    # approval-service refuses a third send-back; should one arrive anyway, the run ends instead of looping.
    await world.decide_message(Decision.SEND_BACK, reason="Again")
    run = await world.run()
    assert len(await world.messages()) == 3 and not await world.events("message.send.requested")
    assert run.status is RunStatus.RESOLVED and "all revisions were used" in run.detail


async def test_choosing_not_to_send_keeps_the_runs_outcome(world: Messages) -> None:
    await world.to_message_gate()
    await world.decide_message(Decision.REJECT, reason="Told them by phone")
    run = await world.run()
    assert not await world.events("message.send.requested")
    assert run.status is RunStatus.RESOLVED and "chose not to send" in run.detail


async def test_failed_or_partial_sends_are_reported_on_the_run(world: Messages) -> None:
    run = await world.to_message_gate()
    await world.decide_message()
    await world.send(
        MessageFailed(
            message_id="m-1", run_id=run.id, thread_id="t-1", approval_id=run.approval_id,
            failed_tokens=["PERSON_1"], reason="SMTP refused the recipient",
        ),
        "mail-service",
    )  # fmt: skip
    await world.advance()
    run = await world.run()
    assert run.status is RunStatus.RESOLVED
    assert run.detail.endswith("Customer message could not be sent (SMTP refused the recipient)")


async def test_result_arriving_before_the_worker_parks_is_kept(world: Messages) -> None:
    run = await world.to_message_gate()
    await world.send(
        ApprovalDecided(
            approval_id=run.approval_id, run_id=run.id, thread_id="t-1", gate=Gate.MESSAGE,
            decision=Decision.APPROVE, decided_by="u-1", decided_by_role="l1_analyst",
        ),
        "approval-service",
    )  # fmt: skip
    await world.sent()  # arrives first; the worker continues at once when it reaches await_send
    assert (await world.run()).status is RunStatus.RESOLVED


async def test_a_resolution_reaches_every_linked_reporter_once(world: Messages) -> None:
    await world.to_fix_gate()
    run = await world.run()
    t = world.c.tables.runs
    async with world.c.sessions() as s, s.begin():
        for n, (email_token, name_token) in enumerate(
            [("EMAIL_4", "PERSON_4"), ("EMAIL_4", "PERSON_4"), ("EMAIL_7", None)], start=2
        ):
            await s.execute(
                t.insert().values(
                    id=f"r-{n}", email_id=f"e-{n}", thread_id=f"t-{n}", status="ticket_created",
                    created_at=NOW, updated_at=NOW, due_at=NOW, attempts=0, jira_key=run.jira_key,
                    reporter={"email_token": email_token, "name_token": name_token, "email_id": f"e-{n}",
                              "site": "SITE-B207"},
                )
            )  # fmt: skip
    await world.decide(Gate.FIX)
    await world.executed()
    await world.advance()
    p = (await world.messages())[0]["proposal"]
    assert [(r["email_token"], r["email_id"], r["how"]) for r in p["recipients"]] == [
        ("EMAIL_4", "e-3", "Linked duplicate"),  # the later of the same person's two reports
        ("EMAIL_7", "e-4", "Linked duplicate"),
        ("EMAIL_1", "e-1", "Reported"),
    ]
    assert next(c for c in p["checks"] if c["id"] == "recipients")["detail"] == "3 of 3"


async def test_a_linked_report_without_a_stored_address_fails_the_recipients_check(world: Messages) -> None:
    await world.to_fix_gate()
    run = await world.run()
    t = world.c.tables.runs
    async with world.c.sessions() as s, s.begin():
        await s.execute(
            t.insert().values(
                id="r-old", email_id="e-old", thread_id="t-old", status="ticket_created", created_at=NOW,
                updated_at=NOW, due_at=NOW, attempts=0, jira_key=run.jira_key,
            )
        )  # fmt: skip
    await world.decide(Gate.FIX)
    await world.executed()
    await world.advance()
    (request,) = await world.messages()
    check = next(c for c in request["proposal"]["checks"] if c["id"] == "recipients")
    assert not check["ok"] and "no stored address" in check["detail"]
    assert request["detail"].endswith("1 check to fix")


async def test_no_fix_with_missing_details_asks_the_customer_and_waits(world: Messages) -> None:
    world.ops.candidates = []
    world.model.answer = NEEDS_DETAILS
    await world.to_fix_gate()
    (request,) = await world.messages()
    p = request["proposal"]
    assert p["kind"] == "follow_up" and "- Upload file name\n- Browser version 12" in p["body"]
    assert all(c["ok"] for c in p["checks"])  # figures in our own questions are not "invented"
    await world.decide_message()
    await world.sent()
    run = await world.run()
    assert run.status is RunStatus.WAITING_CUSTOMER
    assert run.detail.startswith("No allow-listed fix") and run.detail.endswith("Customer message sent")


async def test_rejected_fix_goes_to_comms_as_an_acknowledgement(world: Messages) -> None:
    await world.to_fix_gate()
    await world.decide(Gate.FIX, Decision.REJECT, reason="Engineering is already on it")
    await world.advance()
    (request,) = await world.messages()
    assert request["proposal"]["kind"] == "acknowledgement"
    assert (
        "Engineering is already on it" in world.comms.briefs[0] and "not fixed yet" in world.comms.briefs[0]
    )
    await world.decide_message()
    await world.sent()
    run = await world.run()
    assert run.status is RunStatus.FIX_REJECTED and run.detail.startswith("Engineering is already on it")


async def test_failed_fix_acknowledges_only_this_reporter(world: Messages) -> None:
    await world.to_fix_gate()
    await world.decide(Gate.FIX)
    await world.advance()
    world.ops.healthy_after = False
    await world.executed()
    await world.advance()
    (request,) = await world.messages()
    assert request["proposal"]["kind"] == "acknowledgement"
    assert len(request["proposal"]["recipients"]) == 1
    assert [e["status"] for e in await world.events("run.status_changed")].count("fix_failed") == 1


async def test_linked_report_waits_for_jira_then_is_acknowledged(world: Messages) -> None:
    world.c.ports.issues.recent = [
        {"key": "KAN-3", "summary": "[SITE-A014] Upload broken", "status": "In Progress"}
    ]  # type: ignore[attr-defined]
    await world.send(
        EmailReceived(email_id="e-1", thread_id="t-1", provider="imap", received_at=NOW), "mail-service"
    )
    await world.advance()
    run = await world.run()
    await world.decide(Gate.TICKET, decided_by_role="l1_analyst")
    await world.advance()
    assert (await world.run()).node == "await_ticket"
    await world.send(
        JiraIssueLinked(
            thread_id="t-1", issue_key="KAN-3", approval_id=approval_id_for(run.id, "ticket"), run_id=run.id
        ),
        "jira-connector",
    )
    await world.advance()
    run = await world.run()
    assert run.jira_key == "KAN-3" and run.node == "gate_message"
    assert world.ops.queries == []  # a linked report does not start its own diagnostics
    (request,) = await world.messages()
    assert request["proposal"]["kind"] == "acknowledgement" and request["proposal"]["jira_key"] == "KAN-3"
    assert "already know about this problem" in world.comms.briefs[0]


async def test_llm_down_parks_the_draft_and_unreadable_answer_uses_the_template(world: Messages) -> None:
    world.comms.fail = LlmUnavailableError("circuit open", retry_after=1)
    run = await world.to_message_gate()
    assert run.status is RunStatus.WAITING_LLM
    world.comms.fail = DraftUnusableError("could not be read")
    t = world.c.tables.runs
    async with world.c.sessions() as s, s.begin():
        await s.execute(update(t).where(t.c.id == run.id).values(due_at=NOW))
    await world.advance()
    (request,) = await world.messages()
    p = request["proposal"]
    assert p["written_by"] == "template" and "has been fixed" in p["body"]
    assert all(c["ok"] for c in p["checks"])


async def test_model_inventing_a_figure_or_a_promise_fails_the_checks_for_the_approver(
    world: Messages,
) -> None:
    world.comms.answer = "Fixed at 11:59. It will be back within 2 hours. Your colleague [PERSON_2] knows."
    await world.to_message_gate()
    (request,) = await world.messages()
    failing = {c["id"] for c in request["proposal"]["checks"] if not c["ok"]}
    assert failing == {"facts", "promises", "personal_data"}
    assert request["detail"].endswith("3 checks to fix")


async def test_no_comms_port_no_second_verdict_or_message(world: Messages) -> None:
    p = world.c.ports
    world.c.ports = AgentPorts(
        p.privacy, p.intake, p.embedder, p.issues, p.events, p.threads, p.ops, p.advisor
    )
    world.advance, _ = world.c.runner(InMemorySaver())
    await world.to_fix_gate()
    await world.decide(Gate.FIX)
    await world.executed()
    await world.advance()
    assert (await world.run()).status is RunStatus.RESOLVED and not await world.messages()


def test_templates_pass_their_own_rules() -> None:
    assert kind_for("resolved", SUPPORT) == "resolution"
    assert kind_for("fix_failed", SUPPORT) == "acknowledgement"
    for kind in ("resolution", "follow_up", "acknowledgement"):
        assert "soon" not in fallback_text(kind, "no_fix", "KAN-1")


def test_subject_with_a_token_falls_back_to_the_ticket_summary() -> None:
    assert subject_for({"subject": "Re: [PERSON_1] cannot log in"}, SUPPORT, "KAN-1") == (
        "Re: Upload stuck at 0% [KAN-1]"
    )
    assert subject_for({"subject": "AW: Login [KAN-1]"}, SUPPORT, "KAN-1") == "Re: Login [KAN-1]"


def test_model_text_is_cleaned_of_what_we_add_ourselves() -> None:
    who = {"name_token": "PERSON_1", "email_token": "EMAIL_1"}
    text = clean_model_text(f"Dear [PERSON_1],\n\nFixed, [PERSON_1].\n\nBest regards\n{AI_DISCLOSURE}", who)
    assert text == f"Fixed, {RECIPIENT_TOKEN}."
    assert clean_model_text("Regards", who) == ""


_ = JiraIssueCreated  # the fix-gate helpers send it
