"""approval-service use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opspilot_common.auth.principal import Principal
from opspilot_common.drafts import Check
from opspilot_common.events.catalogue import (
    ActionExecuted,
    ApprovalDecided,
    ApprovalReminder,
    ApprovalRequested,
    Decision,
    Gate,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
    MessageFailed,
    MessageSent,
    RunStatusChanged,
)
from opspilot_common.telemetry import current_traceparent, span

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
                    trace=current_traceparent(),
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
            approval = await uow.approvals.get_for_decision(approval_id)
            if approval is None:
                raise NotFoundError(approval_id)
            if approval.decide(principal, decision, reason=reason, edits=edits or {}, now=self._clock.now()):
                await uow.approvals.save(approval)
                with span("approval.decided", traceparent=approval.trace, approval_id=approval.id):
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
        async with self._uow() as uow:
            if decided:
                items = await uow.approvals.decided(gate=gate)
            else:
                items = await uow.approvals.list(statuses={Status.PENDING}, gate=gate)
            waiting = await uow.approvals.waiting()
        oldest = waiting.oldest_requested_at
        return QueueView(
            items=items,
            waiting=waiting.total,
            waiting_by_gate={g.value: waiting.by_gate.get(g.value, 0) for g in Gate},
            oldest_waiting_seconds=int((self._clock.now() - oldest).total_seconds()) if oldest else None,
            sev1_waiting=waiting.sev1,
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


class CheckDraft:
    """The draft checks on an approver's edits, before they decide, so the screen never shows stale ticks."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, principal: Principal, approval_id: str, edits: dict[str, Any]) -> list[Check]:
        _require_viewer(principal)
        async with self._uow() as uow:
            approval = await uow.approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(approval_id)
        return approval.message_checks(edits)


class RecordMessageOutcome:
    """mail-service's result for an approved message gate."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: MessageSent | MessageFailed) -> None:
        if p.approval_id is None:
            return
        async with self._uow() as uow:
            approval = await uow.approvals.get(p.approval_id)
            if approval is None or approval.gate is not Gate.MESSAGE:
                return
            if isinstance(p, MessageSent):
                approval.message_result(p.recipient_tokens, p.failed_tokens, p.detail)
            else:
                approval.message_result([], p.failed_tokens, p.reason)
            await uow.approvals.save(approval)
            await uow.commit()


class RecordFixOutcome:
    """ops-service's result for an approved fix gate."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: ActionExecuted) -> None:
        async with self._uow() as uow:
            approval = await uow.approvals.get(p.approval_id)
            if approval is None or approval.gate is not Gate.FIX:
                return
            approval.fix_executed(p.ok, p.detail)
            await uow.approvals.save(approval)
            await uow.commit()


_VERDICTS = {"resolved": True, "fix_failed": False}  # run status after an approved fix → health passed?


class RecordFixHealth:
    """`run.status_changed` resolved / fix_failed after an approved fix: what the health re-check said, so
    the fix screen stops promising a check that already happened."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: RunStatusChanged) -> None:
        if p.status not in _VERDICTS:
            return
        async with self._uow() as uow:
            approval = await uow.approvals.latest_for_run(p.run_id, Gate.FIX)
            if approval and approval.health_checked(_VERDICTS[p.status], p.detail):
                await uow.approvals.save(approval)
                await uow.commit()


class SendReminders:
    """Every minute: `approval.reminder` for gates that have waited too long (T2: every 15 min)."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self) -> int:
        now = self._clock.now()
        sent = 0
        async with self._uow() as uow:
            for approval in await uow.approvals.list(statuses={Status.PENDING}, gate=Gate.FIX):
                if not approval.remind(now):
                    continue
                await uow.approvals.save(approval)
                uow.events.record(
                    ApprovalReminder(
                        approval_id=approval.id,
                        gate=approval.gate,
                        waiting_seconds=int((now - approval.requested_at).total_seconds()),
                        run_id=approval.run_id,
                        thread_id=approval.thread_id,
                        jira_key=approval.jira_key,
                        title=approval.title,
                        reminder=approval.reminders,
                    ),
                    actor="approval-service",
                )
                sent += 1
            await uow.commit()
        return sent
