"""An approval: one gate a person must pass before OpsPilot acts. Decisions are final and attributable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import GATE_APPROVERS, Role, can_decide
from opspilot_common.drafts import MAX_REVISIONS, Check, blocking, check_draft
from opspilot_common.events.catalogue import Decision, Gate

from .errors import AlreadyDecidedError, InvalidDecisionError, NotAllowedError

# Who may see the queue (deciding is per gate, GATE_APPROVERS). Admins run the system, not cases.
VIEWERS = frozenset({Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER, Role.COMPLIANCE_LEAD})
SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
MAX_BODY = 8000
# What an approver may change before approving, per gate.
EDITABLE: dict[Gate, frozenset[str]] = {
    Gate.TICKET: frozenset({"summary", "severity"}),
    Gate.FIX: frozenset({"params"}),
    Gate.MESSAGE: frozenset({"body", "subject"}),
}


# Gate T2 waits for a named support agent: remind them every 15 minutes while it waits (wireframe).
REMIND_AFTER: dict[Gate, timedelta] = {Gate.FIX: timedelta(minutes=15)}


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
    """What happened after an approved gate (fed by jira-connector and ops-service events)."""

    TICKET_PENDING = "ticket_pending"  # approved; Jira not reached yet (e.g. circuit open)
    TICKET_CREATED = "ticket_created"
    LINKED = "linked"
    TICKET_FAILED = "ticket_failed"
    FIX_PENDING = "fix_pending"  # approved; ops-service is running it
    FIX_DONE = "fix_done"
    FIX_FAILED = "fix_failed"  # refused at run time or failed: nothing (more) happens without a person
    MESSAGE_PENDING = "message_pending"  # approved; mail-service is sending it
    MESSAGE_SENT = "message_sent"
    MESSAGE_PARTLY_SENT = "message_partly_sent"  # some recipients could not be reached
    MESSAGE_FAILED = "message_failed"  # reached nobody: a person has to act


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
    reminders: int = 0
    reminded_at: datetime | None = None
    health: str | None = None  # T2 after the fix ran: "resolved" | "fix_failed" (the health re-check)
    health_detail: str | None = None

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
        if decision is Decision.SEND_BACK and int(self.proposal.get("revision") or 0) >= MAX_REVISIONS:
            raise InvalidDecisionError(
                f"All {MAX_REVISIONS} revisions are used: edit the draft yourself, or choose not to send it."
            )
        reason = " ".join((reason or "").split()) or None
        if decision is not Decision.APPROVE and not reason:
            raise InvalidDecisionError("Say why, so the team and the audit log know.")
        clean = self._valid_edits(edits) if decision is Decision.APPROVE else {}
        failing = (
            blocking(self.message_checks(clean))
            if decision is Decision.APPROVE and self.gate is Gate.MESSAGE
            else []
        )
        if failing:
            names = "; ".join(f"{c.label}: {c.detail}" for c in failing)
            raise InvalidDecisionError(f"This message cannot be sent yet. {names}.")
        self.status, self.decided_at, self.decided_by = _RESULT[decision], now, principal.user_id
        self.decided_by_role = sorted(principal.roles & GATE_APPROVERS[self.gate])[0].value
        self.reason, self.edits = reason, clean
        if "severity" in clean:
            self.severity = clean["severity"]  # the approver's call replaces the AI's suggestion
        if decision is Decision.APPROVE and self.gate is Gate.TICKET:
            self.outcome = Outcome.TICKET_PENDING
        if decision is Decision.APPROVE and self.gate is Gate.FIX:
            self.outcome = Outcome.FIX_PENDING
        if decision is Decision.APPROVE and self.gate is Gate.MESSAGE:
            self.outcome = Outcome.MESSAGE_PENDING
        return True

    def message_checks(self, edits: dict[str, Any]) -> list[Check]:
        """The draft checks on the text as it would go out, with the approver's edits applied."""
        if self.gate is not Gate.MESSAGE:
            raise InvalidDecisionError("Only customer messages have draft checks.")
        subject, body = str(self.proposal.get("subject") or ""), str(self.proposal.get("body") or "")
        new_subject = str(edits.get("subject") or subject)
        new_body = str(edits.get("body") or body)
        context = self.proposal.get("check_context") or {}
        return check_draft(new_subject, new_body, context, edited=new_body != body or new_subject != subject)

    @property
    def next_reminder_at(self) -> datetime | None:
        """When the next reminder goes out (15, 30, 45 … min after the request), while it waits."""
        every = REMIND_AFTER.get(self.gate)
        if every is None or self.status is not Status.PENDING:
            return None
        return self.requested_at + every * (self.reminders + 1)

    def remind(self, now: datetime) -> bool:
        """True when a reminder is due now (and counts it)."""
        due = self.next_reminder_at
        if due is None or now < due:
            return False
        every = REMIND_AFTER[self.gate]
        self.reminders = int((now - self.requested_at) / every)  # one reminder even after a long outage
        self.reminded_at = now
        return True

    def _valid_edits(self, edits: dict[str, Any]) -> dict[str, Any]:
        unknown = set(edits) - EDITABLE[self.gate]
        if unknown:
            raise InvalidDecisionError(f"These fields cannot be changed here: {', '.join(sorted(unknown))}.")
        if "severity" in edits and edits["severity"] not in SEVERITIES:
            raise InvalidDecisionError("Severity must be sev1, sev2, sev3 or sev4.")
        if "summary" in edits and not 3 <= len(str(edits["summary"]).strip()) <= 240:
            raise InvalidDecisionError("The summary must be 3 to 240 characters.")
        if "subject" in edits and not 3 <= len(str(edits["subject"]).strip()) <= 240:
            raise InvalidDecisionError("The subject must be 3 to 240 characters.")
        if "subject" in edits and any(ch in str(edits["subject"]) for ch in "\r\n"):
            raise InvalidDecisionError("The subject must be one line.")
        if "body" in edits and not 20 <= len(str(edits["body"]).strip()) <= MAX_BODY:
            raise InvalidDecisionError(f"The message must be 20 to {MAX_BODY} characters.")
        if "params" in edits:
            edits = {**edits, "params": self._valid_params(edits["params"])}
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in edits.items()}

    def _valid_params(self, params: Any) -> dict[str, str]:
        """T2: the approver may pick another value only from the choices the proposal offered."""
        if not isinstance(params, dict):
            raise InvalidDecisionError("Parameters must be name/value pairs.")
        action = self.proposal.get("action") or {}
        specs = {str(p.get("name")): p for p in action.get("params_spec", []) if isinstance(p, dict)}
        clean: dict[str, str] = {}
        for name, value in params.items():
            spec = specs.get(str(name))
            if spec is None:
                raise InvalidDecisionError(
                    f"{name} is not a parameter of {action.get('name', 'this action')}."
                )
            if str(value) not in [str(c) for c in spec.get("choices", [])]:
                raise InvalidDecisionError(
                    f"{spec.get('label', name)} '{value}' is not one of the allowed values."
                )
            clean[str(name)] = str(value)
        return clean

    def ticket_created(self, key: str) -> None:
        self.jira_key, self.outcome, self.outcome_detail = key, Outcome.TICKET_CREATED, None

    def linked(self, key: str) -> None:
        self.jira_key, self.outcome, self.outcome_detail = key, Outcome.LINKED, None

    def ticket_failed(self, reason: str) -> None:
        self.outcome, self.outcome_detail = Outcome.TICKET_FAILED, reason

    def fix_executed(self, ok: bool, detail: str) -> None:
        self.outcome, self.outcome_detail = (Outcome.FIX_DONE if ok else Outcome.FIX_FAILED), detail

    def message_result(self, sent: list[str], failed: list[str], detail: str | None) -> None:
        if not sent:
            self.outcome = Outcome.MESSAGE_FAILED
        else:
            self.outcome = Outcome.MESSAGE_PARTLY_SENT if failed else Outcome.MESSAGE_SENT
        self.outcome_detail = detail

    def health_checked(self, resolved: bool, detail: str | None) -> bool:
        """The orchestrator's health re-check after an approved fix. Only an approved T2 gate takes it, once:
        the run announces its outcome again when it ends, after the customer message."""
        if self.gate is not Gate.FIX or self.status is not Status.APPROVED or self.health:
            return False
        self.health, self.health_detail = ("resolved" if resolved else "fix_failed"), detail
        return True
