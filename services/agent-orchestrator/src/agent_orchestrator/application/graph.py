"""The agent workflow for gate T1: privacy_gate → intake → dedupe → request_ticket_approval → gate_ticket.

Routing is deterministic; the 8B model only fills a typed schema (ADR-0004). Nodes get their ports from
`config["configurable"]["ports"]` (never by importing clients). A node with a side effect (publishing the
approval request) comes *before* the `interrupt()` node, because LangGraph re-runs an interrupted node from
its start on resume.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from opspilot_common.events.catalogue import AgentToolCalled, ApprovalRequested, Gate

from ..domain.errors import ContextUnavailableError
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
    approval_id: str
    decision: dict[str, Any]
    outcome: str  # ignored | approved | rejected


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
    decision: dict[str, Any] = interrupt({"approval_id": state["approval_id"], "gate": Gate.TICKET.value})
    return {
        "decision": decision,
        "outcome": "approved" if decision.get("decision") == "approve" else "rejected",
    }


def build_graph() -> StateGraph:  # type: ignore[type-arg]
    graph = StateGraph(TicketState)
    graph.add_node("privacy_gate", privacy_gate)
    graph.add_node("intake", intake)
    graph.add_node("dedupe", dedupe)
    graph.add_node("request_ticket_approval", request_ticket_approval)
    graph.add_node("gate_ticket", gate_ticket)
    graph.add_edge(START, "privacy_gate")
    graph.add_edge("privacy_gate", "intake")
    graph.add_conditional_edges("intake", after_intake, ["dedupe", END])
    graph.add_edge("dedupe", "request_ticket_approval")
    graph.add_edge("request_ticket_approval", "gate_ticket")
    graph.add_edge("gate_ticket", END)
    return graph
