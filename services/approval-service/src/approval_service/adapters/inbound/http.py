"""Approval API for people (through the gateway).

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal
from opspilot_common.events.catalogue import Decision, Gate
from pydantic import BaseModel, Field

from ...domain.approval import Approval
from ...domain.errors import (
    AlreadyDecidedError,
    ApprovalError,
    InvalidDecisionError,
    NotAllowedError,
    NotFoundError,
)

APPROVER_LABEL = {Gate.TICKET: "Analyst", Gate.FIX: "Support agent", Gate.MESSAGE: "Analyst"}


class DecisionBody(BaseModel):
    decision: Decision
    reason: str | None = Field(default=None, max_length=1000)
    edits: dict[str, Any] = Field(default_factory=dict)


def summary_json(a: Approval) -> dict[str, Any]:
    return {
        "id": a.id,
        "run_id": a.run_id,
        "thread_id": a.thread_id,
        "gate": a.gate.value,
        "title": a.title,
        "detail": a.detail,
        "site": a.site,
        "severity": a.severity,
        "approver": APPROVER_LABEL[a.gate],
        "requested_at": a.requested_at.isoformat(),
        "status": a.status.value,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by_role": a.decided_by_role,
        "reason": a.reason,
        "outcome": a.outcome.value if a.outcome else None,
        "outcome_detail": a.outcome_detail,
        "jira_key": a.jira_key,
    }


def detail_json(a: Approval) -> dict[str, Any]:
    return {**summary_json(a), "proposal": a.proposal, "edits": a.edits}


def install_error_handlers(app: FastAPI) -> None:
    codes: dict[type[ApprovalError], int] = {
        NotFoundError: 404,
        NotAllowedError: 403,
        AlreadyDecidedError: 409,
        InvalidDecisionError: 422,
    }

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApprovalError)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=codes.get(type(exc), 400))

    app.add_exception_handler(ApprovalError, handle)


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["approvals"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.get("/approvals")
    async def queue(principal: SignedIn, decided: bool = False, gate: Gate | None = None) -> dict[str, Any]:
        view = await container.list_approvals(principal, decided=decided, gate=gate)
        return {
            "items": [summary_json(a) for a in view.items],
            "stats": {
                "waiting": view.waiting,
                "waiting_by_gate": view.waiting_by_gate,
                "oldest_waiting_seconds": view.oldest_waiting_seconds,
                "sev1_waiting": view.sev1_waiting,
            },
        }

    @router.get("/approvals/{approval_id}")
    async def one(approval_id: str, principal: SignedIn) -> dict[str, Any]:
        return detail_json(await container.get_approval(principal, approval_id))

    @router.post("/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: DecisionBody, principal: SignedIn) -> dict[str, Any]:
        approval = await container.decide(principal, approval_id, body.decision, body.reason, body.edits)
        return detail_json(approval)

    return router
