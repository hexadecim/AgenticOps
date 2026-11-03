"""approval-service use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opspilot_common.auth.principal import Principal
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    ApprovalRequested,
    Decision,
    Gate,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
)

from ..domain.approval import VIEWERS, Approval, Status
from ..domain.errors import NotAllowedError, NotFoundError
from ..ports import Clock, UnitOfWork

UowFactory = Callable[[], UnitOfWork]


def _require_viewer(principal: Principal) -> None:
    if not principal.has_any(*VIEWERS):
        raise NotAllowedError("Your role cannot see the approval queue.")


class RecordRequest:
    """`approval.requested` → a pending approval. Repeats are ignored (the id comes from the run)."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: ApprovalRequested, requested_at: Any = None) -> None:
        async with self._uow() as uow:
            if await uow.approvals.get(p.approval_id):
                return
            await uow.approvals.add(
                Approval(
                    id=p.approval_id,
                    run_id=p.run_id,
                    thread_id=p.thread_id,
                    gate=p.gate,
                    title=p.title,
                    detail=p.detail,
                    requested_at=requested_at or self._clock.now(),
                    site=p.site,
                    severity=p.severity,
                    jira_key=p.jira_key,
                    proposal=p.proposal,
                )
            )
            await uow.commit()


class Decide:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(
        self,
        principal: Principal,
        approval_id: str,
        decision: Decision,
        reason: str | None = None,
        edits: dict[str, Any] | None = None,
    ) -> Approval:
        async with self._uow() as uow:
            approval = await uow.approvals.get(approval_id)
            if approval is None:
                raise NotFoundError(approval_id)
            if approval.decide(principal, decision, reason=reason, edits=edits or {}, now=self._clock.now()):
                await uow.approvals.save(approval)
                uow.events.record(
                    ApprovalDecided(
                        approval_id=approval.id,
                        run_id=approval.run_id,
                        thread_id=approval.thread_id,
                        gate=approval.gate,
                        decision=decision,
                        decided_by=principal.user_id,
                        decided_by_role=approval.decided_by_role or "",
                        reason=approval.reason,
                        edits=approval.edits,
                    ),
                    actor=principal.user_id,
                )
                await uow.commit()
            return approval


@dataclass(frozen=True, slots=True)
class QueueView:
    items: list[Approval]
    waiting: int
    waiting_by_gate: dict[str, int]
    oldest_waiting_seconds: int | None
    sev1_waiting: int


class ListApprovals:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(
        self, principal: Principal, *, decided: bool = False, gate: Gate | None = None
    ) -> QueueView:
        _require_viewer(principal)
        statuses = {Status.APPROVED, Status.REJECTED, Status.SENT_BACK} if decided else {Status.PENDING}
        async with self._uow() as uow:
            items = await uow.approvals.list(statuses=statuses, gate=gate)
            pending = (
                items if not decided and gate is None else await uow.approvals.list(statuses={Status.PENDING})
            )
        now = self._clock.now()
        if decided:
            items = sorted(items, key=lambda a: a.decided_at or a.requested_at, reverse=True)
        return QueueView(
            items=items,
            waiting=len(pending),
            waiting_by_gate={g.value: sum(a.gate is g for a in pending) for g in Gate},
            oldest_waiting_seconds=int((now - pending[0].requested_at).total_seconds()) if pending else None,
            sev1_waiting=sum(a.severity == "sev1" for a in pending),
        )


class GetApproval:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, principal: Principal, approval_id: str) -> Approval:
        _require_viewer(principal)
        async with self._uow() as uow:
            approval = await uow.approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(approval_id)
        return approval


class RecordTicketOutcome:
    """jira-connector's result for an approved ticket gate, so the queue can say what happened."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: JiraIssueCreated | JiraIssueLinked | JiraIssueFailed) -> None:
        if p.approval_id is None:
            return
        async with self._uow() as uow:
            approval = await uow.approvals.get(p.approval_id)
            if approval is None:
                return
            if isinstance(p, JiraIssueCreated):
                approval.ticket_created(p.issue_key)
            elif isinstance(p, JiraIssueLinked):
                approval.linked(p.issue_key)
            else:
                approval.ticket_failed(p.reason)
            await uow.approvals.save(approval)
            await uow.commit()
