"""The agent workflow:
privacy_gate → intake → dedupe → request_ticket_approval → gate_ticket (T1) → await_ticket → diagnostics
→ request_fix_approval → gate_fix (T2) → await_fix → health_check → draft_message
→ request_message_approval → gate_message (T3) → request_send → await_send. A run owes the customer one
message, at the point where it has something to say (ADR-0007), so a T3 waiting for a person never holds up
a ticket or a fix.

Routing is deterministic; the 8B model only fills a typed schema (ADR-0004). Nodes get their ports from
`config["configurable"]["ports"]` (never by importing clients). A node with a side effect (publishing the
approval request) comes *before* the `interrupt()` node, because LangGraph re-runs an interrupted node from
its start on resume.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opspilot_common.drafts import MAX_REVISIONS
from opspilot_common.events.catalogue import (
    AgentToolCalled,
    ApprovalRequested,
    Gate,
    MessageRecipient,
    MessageSendRequested,
    RunStatusChanged,
)

from ..domain.comms import (
    FOLLOW_UP,
    RESOLUTION,
    assemble_body,
    build_message_proposal,
    clean_model_text,
    display_token,
    draft_brief,
    fallback_text,
    kind_for,
    known_facts,
    message_detail,
    recipients_for,
    reporter_of,
    subject_for,
)
from ..domain.diagnosis import (
    build_fix_proposal,
    candidate_for,
    diagnostics_detail,
    health_verdict,
    problem_text,
    render_evidence,
)
from ..domain.errors import ContextUnavailableError, DiagnosisUnusableError, DraftUnusableError
from ..domain.triage import (
    DEDUPE_HOURS,
    DUPLICATE_THRESHOLD,
    Intake,
    approval_id_for,
    best_match,
    build_proposal,
    dedupe_detail,
    ticket_summary,
)
from ..ports import AgentPorts


class TicketState(TypedDict, total=False):
    run_id: str
    email_id: str
    thread_id: str  # the mail thread
    email: dict[str, Any]
    privacy: dict[str, Any]
    intake: dict[str, Any]
    model: dict[str, Any]
    jira: dict[str, Any]
    dedupe: dict[str, Any]
    proposal: dict[str, Any]
    approval_id: str  # the gate the run waits at now
    decision: dict[str, Any]
    jira_key: str
    diagnostics: dict[str, Any]
    fix_proposal: dict[str, Any]
    fix_note: str
    fix_approval_id: str
    fix_decision: dict[str, Any]
    fix_result: dict[str, Any]
    health: dict[str, Any]
    message: dict[str, Any] | None  # the draft at gate T3 (None: this run sends nothing)
    message_revision: int  # how many times the draft was sent back
    message_notes: list[str]  # the approver's notes for Comms
    message_approval_id: str
    message_decision: dict[str, Any]
    send_result: dict[str, Any]
    # sent | partly_sent | not_sent | rejected | revisions_used; unset when no message was drafted
    message_outcome: str
    # ignored | linked | rejected | no_fix | fix_rejected | resolved | fix_failed
    outcome: str


def _ports(config: RunnableConfig) -> AgentPorts:
    ports = (config.get("configurable") or {}).get("ports")
    assert isinstance(ports, AgentPorts), "ports must be injected via config['configurable']['ports']"
    return ports


async def privacy_gate(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    """Fail closed: if privacy-service cannot pseudonymise, the run waits; it never reads raw mail."""
    email, privacy = await _ports(config).privacy.pseudonymise(state["email_id"])
    return {"email": email, "privacy": privacy}


async def intake(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ports = _ports(config)
    result = await ports.intake.classify(state["email"])
    model = await ports.intake.describe()
    verdict = (
        f"{result.category}, {result.severity}"
        + (
            f", missing: {', '.join(result.missing_details)}"
            if result.missing_details
            else ", nothing missing"
        )
        if result.is_support_request
        else "not a support request; no ticket proposed"
    )
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            agent="Intake",
            tool="classify",
            detail=f"{verdict} ({model.get('model')}, thinking {'on' if model.get('thinking') else 'off'})",
        ),
        actor="Intake",
    )
    out: dict[str, Any] = {"intake": result.model_dump(), "model": model}
    if not result.is_support_request:
        out["outcome"] = "ignored"
    elif reporter := reporter_of(state["email"], state["email_id"], result.site):
        await ports.threads.remember_reporter(state["run_id"], reporter)
    return out


def after_intake(state: TicketState) -> str:
    return END if state.get("outcome") == "ignored" else "dedupe"


async def dedupe(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ports = _ports(config)
    result: dict[str, Any] = {
        "checked": False, "searched": 0, "hours": DEDUPE_HOURS, "threshold": DUPLICATE_THRESHOLD,
        "best": None, "duplicate_of": None, "note": None,
    }  # fmt: skip
    jira: dict[str, Any] = {"configured": False}
    same_thread = await ports.threads.ticket_for_thread(state["thread_id"], exclude_run=state["run_id"])
    if same_thread:
        result |= {
            "checked": True,
            "duplicate_of": {"key": same_thread, "score": 1.0, "reason": "same email thread"},
        }
    try:
        ctx = await ports.issues.context(DEDUPE_HOURS)
        jira = {k: ctx.get(k) for k in ("configured", "project", "issue_type")}
        recent = ctx.get("recent", []) if ctx.get("configured") else []
        if not ctx.get("configured"):
            result["note"] = "Jira is not configured"
        elif not same_thread:
            result |= {"checked": True, "searched": len(recent)}
            if recent:
                summary = ticket_summary(Intake.model_validate(state["intake"]))
                vectors = await ports.embedder.embed([summary] + [i["summary"] for i in recent])
                best = best_match(vectors[0], list(zip(recent, vectors[1:], strict=True)))
                if best:
                    issue, score = best
                    result["best"] = {
                        "key": issue["key"],
                        "summary": issue["summary"],
                        "score": round(score, 2),
                    }
                    if score >= DUPLICATE_THRESHOLD:
                        result["duplicate_of"] = {**result["best"], "reason": "similar open incident"}
    except ContextUnavailableError as exc:
        result["note"] = f"Jira search unavailable: {exc.message}"
    best = result["best"]
    searched = f"Searched {result['searched']} open incidents from the last {DEDUPE_HOURS} h"
    detail = (
        f"{searched}; closest {best['key']} {best['score']:.2f} (threshold {DUPLICATE_THRESHOLD:.2f})"
        if best
        else (searched if result["checked"] else (result["note"] or "Duplicates not checked"))
    )
    if result["duplicate_of"]:
        detail += f"; proposing to link to {result['duplicate_of']['key']}"
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"], thread_id=state["thread_id"], agent="Triage", tool="search_jira",
            detail=detail, privacy="Token and site ID only",
        ),
        actor="Triage",
    )  # fmt: skip
    return {"dedupe": result, "jira": jira}


async def request_ticket_approval(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ports = _ports(config)
    found = Intake.model_validate(state["intake"])
    proposal = build_proposal(
        email=state["email"], privacy=state.get("privacy", {}), intake=found, dedupe=state["dedupe"],
        jira=state.get("jira", {}), model=state.get("model", {}),
    )  # fmt: skip
    approval_id = approval_id_for(state["run_id"], Gate.TICKET.value)
    await ports.events.emit(
        ApprovalRequested(
            approval_id=approval_id,
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            gate=Gate.TICKET,
            title=str(state["email"].get("subject") or found.summary)[:200],
            detail=f"Intake: {found.category} · {dedupe_detail(state['dedupe'])}",
            site=found.site,
            severity=found.severity,
            jira_key=(state["dedupe"].get("duplicate_of") or {}).get("key"),
            proposal=proposal,
        ),
        actor="Triage",
    )
    return {"proposal": proposal, "approval_id": approval_id}


async def gate_ticket(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    decision: dict[str, Any] = interrupt(
        {"wait": "approval", "approval_id": state["approval_id"], "gate": Gate.TICKET.value}
    )
    return {
        "decision": decision,
        "outcome": "approved" if decision.get("decision") == "approve" else "rejected",
    }


def after_ticket_gate(state: TicketState) -> str:
    return "await_ticket" if state.get("outcome") == "approved" else END


async def await_ticket(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ticket: dict[str, Any] = interrupt({"wait": "ticket", "detail": "Ticket approved; creating it in Jira"})
    linked = (state.get("proposal") or {}).get("action") == "link"
    return {"jira_key": ticket.get("jira_key"), "outcome": "linked" if linked else "ticket_created"}


def after_ticket(state: TicketState) -> str:
    """A new incident goes on to diagnostics; a report linked to an open ticket only needs an answer."""
    return "draft_message" if state.get("outcome") == "linked" else "diagnostics"


async def diagnostics(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    """Evidence from ops-service, then the model picks one allow-listed candidate (or none)."""
    ports = _ports(config)
    if ports.ops is None or ports.advisor is None:
        return {"outcome": "no_fix", "fix_note": "Diagnostics is not set up"}
    found = Intake.model_validate(state["intake"])
    key = state.get("jira_key")
    d = await ports.ops.diagnose(
        text=problem_text(found), site=found.site, asset=found.asset, run_id=state["run_id"],
        thread_id=state["thread_id"],
    )  # fmt: skip
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"], thread_id=state["thread_id"], agent="Diagnostics", tool="gather_evidence",
            detail=diagnostics_detail(d), privacy="Masked logs, tokens only", jira_key=key,
        ),
        actor="Diagnostics",
    )  # fmt: skip
    candidates: list[dict[str, Any]] = d.get("candidates", [])
    if not candidates:
        note = "No allow-listed fix in the runbooks fits this problem"
        if not any(not p.get("ok") for p in d.get("probes", [])):
            note += "; all probes pass"
        return {"diagnostics": d, "outcome": "no_fix", "fix_note": note}
    try:
        choice = await ports.advisor.choose(render_evidence(found, key, d), len(candidates))
    except DiagnosisUnusableError as exc:
        return {"diagnostics": d, "outcome": "no_fix", "fix_note": f"No fix proposed: {exc.message}"}
    candidate = candidate_for(choice, candidates)
    verdict = (
        f"Proposing {candidate['signature']} ({choice.confidence} confidence): {choice.likely_cause}"
        if candidate
        else f"No fix proposed: {choice.likely_cause}"
    )
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"], thread_id=state["thread_id"], agent="Diagnostics", tool="propose_fix",
            detail=verdict[:500], privacy="Masked logs, tokens only", jira_key=key,
        ),
        actor="Diagnostics",
    )  # fmt: skip
    if candidate is None:
        return {
            "diagnostics": d,
            "outcome": "no_fix",
            "fix_note": f"The evidence supports none of the allow-listed fixes: {choice.likely_cause}",
        }
    proposal = build_fix_proposal(
        candidate=candidate, choice=choice, diagnostics=d, jira_key=key,
        reporters=await ports.threads.reporters(key) if key else 1, model=state.get("model", {}),
    )  # fmt: skip
    return {"diagnostics": d, "fix_proposal": proposal}


def after_diagnostics(state: TicketState) -> str:
    return "draft_message" if state.get("outcome") == "no_fix" else "request_fix_approval"


async def request_fix_approval(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ports = _ports(config)
    p = state["fix_proposal"]
    found = Intake.model_validate(state["intake"])
    approval_id = approval_id_for(state["run_id"], Gate.FIX.value)
    rb = p["runbook"]
    await ports.events.emit(
        ApprovalRequested(
            approval_id=approval_id,
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            gate=Gate.FIX,
            title=str(p["action"].get("title") or f"Run {p['action']['signature']}?")[:200],
            detail=f"Diagnostics: {p['diagnosis']['likely_cause']} · runbook {rb['id']} step {rb['step']}"[
                :500
            ],
            site=found.site,
            severity=found.severity,
            jira_key=state.get("jira_key"),
            proposal=p,
        ),
        actor="Diagnostics",
    )
    return {"fix_approval_id": approval_id, "approval_id": approval_id}


async def gate_fix(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    decision: dict[str, Any] = interrupt(
        {"wait": "approval", "approval_id": state["fix_approval_id"], "gate": Gate.FIX.value}
    )
    approved = decision.get("decision") == "approve"
    return {"fix_decision": decision, "outcome": "fix_approved" if approved else "fix_rejected"}


def after_fix_gate(state: TicketState) -> str:
    return "await_fix" if state.get("outcome") == "fix_approved" else "draft_message"


async def await_fix(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    action = state["fix_proposal"]["action"]
    result: dict[str, Any] = interrupt(
        {"wait": "fix", "detail": f"Fix approved; ops-service is running {action['name']}"}
    )
    return {"fix_result": result}


async def health_check(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    """Fresh probes after the action: only a pass makes the incident resolved."""
    ports = _ports(config)
    assert ports.ops is not None
    fix = state.get("fix_result", {})
    probes = await ports.ops.probes(
        state["fix_proposal"].get("region"), run_id=state["run_id"], thread_id=state["thread_id"]
    )
    healthy, verdict = health_verdict(probes)
    ran = fix.get("signature") or state["fix_proposal"]["action"]["signature"]
    detail = f"After {ran}: {verdict}" if fix.get("ok") else f"{ran} did not succeed: {fix.get('detail')}"
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"], thread_id=state["thread_id"], agent="Health", tool="recheck_health",
            detail=detail[:500], privacy="No personal data", jira_key=state.get("jira_key"),
        ),
        actor="Health",
    )  # fmt: skip
    resolved = bool(fix.get("ok")) and healthy
    outcome = "resolved" if resolved else "fix_failed"
    if ports.comms is not None:
        # The verdict is news now, not when a person has approved the customer message.
        await ports.events.emit(
            RunStatusChanged(
                run_id=state["run_id"], thread_id=state["thread_id"], status=outcome, node="health_check",
                detail=verdict, jira_key=state.get("jira_key"), email_id=state["email_id"],
            ),
            actor="Health",
        )  # fmt: skip
    return {"health": {"ok": healthy, "detail": verdict, "probes": probes}, "outcome": outcome}


async def draft_message(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    """The model writes the text (a labelled template if its answer cannot be read); the outcome of the run
    decides the kind of message and its recipients, and the checks are plain code."""
    ports = _ports(config)
    found = Intake.model_validate(state["intake"])
    own = reporter_of(state["email"], state["email_id"], found.site)
    if ports.comms is None or own is None:
        return {"message": None}
    key, outcome = state.get("jira_key"), state.get("outcome")
    kind = kind_for(outcome, found)
    linked = (
        await ports.threads.linked_reporters(key, exclude_run=state["run_id"])
        if key and kind == RESOLUTION
        else []
    )
    recipients, should_reach = recipients_for(kind, own, linked)
    facts, notes = known_facts(dict(state), found), list(state.get("message_notes") or [])
    try:
        written = clean_model_text(await ports.comms.write(draft_brief(kind, dict(state), facts, notes)), own)
    except DraftUnusableError:
        written = ""
    text = written or fallback_text(kind, outcome, key)
    subject = subject_for(state["email"], found, key)
    allowed = [*facts, f"Subject: {subject}"]
    if kind == FOLLOW_UP:
        allowed += [f"Needed: {item}" for item in found.missing_details]
    revision = int(state.get("message_revision") or 0)
    proposal = build_message_proposal(
        kind=kind, subject=subject, body=assemble_body(text, kind, found, key), recipients=recipients,
        should_reach=should_reach, facts=allowed, notes=notes, revision=revision,
        written_by="model" if written else "template", jira_key=key, model=state.get("model", {}),
    )  # fmt: skip
    await ports.events.emit(
        AgentToolCalled(
            run_id=state["run_id"], thread_id=state["thread_id"], agent="Comms", tool="draft_message",
            detail=message_detail(proposal)[:500], jira_key=key,
        ),
        actor="Comms",
    )  # fmt: skip
    return {"message": proposal}


def after_draft(state: TicketState) -> str:
    return "request_message_approval" if state.get("message") else END


async def request_message_approval(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    ports = _ports(config)
    p = state["message"]
    assert p is not None
    found = Intake.model_validate(state["intake"])
    approval_id = approval_id_for(state["run_id"], f"{Gate.MESSAGE.value}:{p['revision']}")
    key = p["jira_key"]
    await ports.events.emit(
        ApprovalRequested(
            approval_id=approval_id,
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            gate=Gate.MESSAGE,
            title=f"{p['kind_label']} for {key}" if key else p["kind_label"],
            detail=message_detail(p),
            site=found.site,
            severity=found.severity,
            jira_key=key,
            proposal=p,
        ),
        actor="Comms",
    )
    return {"message_approval_id": approval_id, "approval_id": approval_id}


async def gate_message(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    decision: dict[str, Any] = interrupt(
        {"wait": "approval", "approval_id": state["message_approval_id"], "gate": Gate.MESSAGE.value}
    )
    out: dict[str, Any] = {"message_decision": decision}
    if decision.get("decision") == "send_back":
        revision = int(state.get("message_revision") or 0)
        if revision >= MAX_REVISIONS:  # approval-service refuses this first; never loop on it
            return {**out, "message_outcome": "revisions_used"}
        notes = [*(state.get("message_notes") or []), str(decision.get("reason") or "")]
        return {**out, "message_revision": revision + 1, "message_notes": notes}
    if decision.get("decision") != "approve":
        return {**out, "message_outcome": "rejected"}
    return out


def after_message_gate(state: TicketState) -> str:
    decision = (state.get("message_decision") or {}).get("decision")
    if decision == "approve":
        return "request_send"
    return "draft_message" if not state.get("message_outcome") else END


async def request_send(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    """Only reached after an approve decision. The approver's edits replace the draft's text."""
    ports = _ports(config)
    p = state["message"]
    assert p is not None
    edits = (state.get("message_decision") or {}).get("edits") or {}
    approval_id = state["message_approval_id"]
    await ports.events.emit(
        MessageSendRequested(
            message_id=str(uuid.uuid5(uuid.UUID(approval_id), "message")),
            run_id=state["run_id"],
            thread_id=state["thread_id"],
            approval_id=approval_id,
            recipient_tokens=[display_token(r) for r in p["recipients"]],
            recipients=[MessageRecipient.model_validate(r) for r in p["recipients"]],
            subject=str(edits.get("subject") or p["subject"]),
            body=str(edits.get("body") or p["body"]),
            jira_key=p["jira_key"],
            kind=p["kind"],
        ),
        actor="Comms",
    )
    return {}


async def await_send(state: TicketState, config: RunnableConfig) -> dict[str, Any]:
    result: dict[str, Any] = interrupt({"wait": "send", "detail": "Message approved; sending it"})
    outcome = "not_sent" if not result.get("sent") else "partly_sent" if result.get("failed") else "sent"
    return {"send_result": result, "message_outcome": outcome}


def build_graph() -> StateGraph:  # type: ignore[type-arg]
    graph = StateGraph(TicketState)
    for name, node in (
        ("privacy_gate", privacy_gate),
        ("intake", intake),
        ("dedupe", dedupe),
        ("request_ticket_approval", request_ticket_approval),
        ("gate_ticket", gate_ticket),
        ("await_ticket", await_ticket),
        ("diagnostics", diagnostics),
        ("request_fix_approval", request_fix_approval),
        ("gate_fix", gate_fix),
        ("await_fix", await_fix),
        ("health_check", health_check),
        ("draft_message", draft_message),
        ("request_message_approval", request_message_approval),
        ("gate_message", gate_message),
        ("request_send", request_send),
        ("await_send", await_send),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "privacy_gate")
    graph.add_edge("privacy_gate", "intake")
    graph.add_conditional_edges("intake", after_intake, ["dedupe", END])
    graph.add_edge("dedupe", "request_ticket_approval")
    graph.add_edge("request_ticket_approval", "gate_ticket")
    graph.add_conditional_edges("gate_ticket", after_ticket_gate, ["await_ticket", END])
    graph.add_conditional_edges("await_ticket", after_ticket, ["diagnostics", "draft_message"])
    graph.add_conditional_edges("diagnostics", after_diagnostics, ["request_fix_approval", "draft_message"])
    graph.add_edge("request_fix_approval", "gate_fix")
    graph.add_conditional_edges("gate_fix", after_fix_gate, ["await_fix", "draft_message"])
    graph.add_edge("await_fix", "health_check")
    graph.add_edge("health_check", "draft_message")
    graph.add_conditional_edges("draft_message", after_draft, ["request_message_approval", END])
    graph.add_edge("request_message_approval", "gate_message")
    graph.add_conditional_edges("gate_message", after_message_gate, ["request_send", "draft_message", END])
    graph.add_edge("request_send", "await_send")
    graph.add_edge("await_send", END)
    return graph
