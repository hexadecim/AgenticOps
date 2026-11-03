"""An approval: one gate a person must pass before OpsPilot acts. Decisions are final and attributable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import GATE_APPROVERS, Role, can_decide
from opspilot_common.events.catalogue import Decision, Gate

from .errors import AlreadyDecidedError, InvalidDecisionError, NotAllowedError

# Who may see the queue (deciding is per gate, GATE_APPROVERS). Admins run the system, not cases.
VIEWERS = frozenset({Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER, Role.COMPLIANCE_LEAD})
SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
# What an approver may change before approving, per gate.
EDITABLE: dict[Gate, frozenset[str]] = {
    Gate.TICKET: frozenset({"summary", "severity"}),
    Gate.FIX: frozenset({"params"}),
    Gate.MESSAGE: frozenset({"body", "subject"}),
}


class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT_BACK = "sent_back"


_RESULT = {
    Decision.APPROVE: Status.APPROVED,
    Decision.REJECT: Status.REJECTED,
    Decision.SEND_BACK: Status.SENT_BACK,
}


class Outcome(StrEnum):
    """What happened after an approved ticket gate (fed by jira-connector events)."""

    TICKET_PENDING = "ticket_pending"  # approved; Jira not reached yet (e.g. circuit open)
    TICKET_CREATED = "ticket_created"
    LINKED = "linked"
    TICKET_FAILED = "ticket_failed"


@dataclass(slots=True)
class Approval:
    id: str
    run_id: str
    thread_id: str
    gate: Gate
    title: str
    detail: str
    requested_at: datetime
    site: str | None = None
    severity: str | None = None
    jira_key: str | None = None
    proposal: dict[str, Any] = field(default_factory=dict)
    status: Status = Status.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None
    decided_by_role: str | None = None
    reason: str | None = None
    edits: dict[str, Any] = field(default_factory=dict)
    outcome: Outcome | None = None
    outcome_detail: str | None = None

    def decide(
        self,
        principal: Principal,
        decision: Decision,
        *,
        reason: str | None,
        edits: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Record the decision. True when it is new; False for a harmless repeat of the same decision."""
        if not can_decide(principal.roles, self.gate):
            raise NotAllowedError("Your role cannot decide this kind of approval.")
        if self.status is not Status.PENDING:
            if self.decided_by == principal.user_id and self.status is _RESULT[decision]:
                return False
            raise AlreadyDecidedError(self.status.value)
        if decision is Decision.SEND_BACK and self.gate is not Gate.MESSAGE:
            raise InvalidDecisionError("Only customer messages can be sent back for revision.")
        reason = " ".join((reason or "").split()) or None
        if decision is not Decision.APPROVE and not reason:
            raise InvalidDecisionError("Say why, so the team and the audit log know.")
        clean = self._valid_edits(edits) if decision is Decision.APPROVE else {}
        self.status, self.decided_at, self.decided_by = _RESULT[decision], now, principal.user_id
        self.decided_by_role = sorted(principal.roles & GATE_APPROVERS[self.gate])[0].value
        self.reason, self.edits = reason, clean
        if "severity" in clean:
            self.severity = clean["severity"]  # the approver's call replaces the AI's suggestion
        if decision is Decision.APPROVE and self.gate is Gate.TICKET:
            self.outcome = Outcome.TICKET_PENDING
        return True

    def _valid_edits(self, edits: dict[str, Any]) -> dict[str, Any]:
        unknown = set(edits) - EDITABLE[self.gate]
        if unknown:
            raise InvalidDecisionError(f"These fields cannot be changed here: {', '.join(sorted(unknown))}.")
        if "severity" in edits and edits["severity"] not in SEVERITIES:
            raise InvalidDecisionError("Severity must be sev1, sev2, sev3 or sev4.")
        if "summary" in edits and not 3 <= len(str(edits["summary"]).strip()) <= 240:
            raise InvalidDecisionError("The summary must be 3 to 240 characters.")
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in edits.items()}

    def ticket_created(self, key: str) -> None:
        self.jira_key, self.outcome, self.outcome_detail = key, Outcome.TICKET_CREATED, None

    def linked(self, key: str) -> None:
        self.jira_key, self.outcome, self.outcome_detail = key, Outcome.LINKED, None

    def ticket_failed(self, reason: str) -> None:
        self.outcome, self.outcome_detail = Outcome.TICKET_FAILED, reason
