"""A run: one email's way through the agent workflow. The graph's progress lives in the checkpointer; the run
row is what people and the worker see (status, why it waits, what came out)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

MAX_ATTEMPTS = 3


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PRIVACY = "waiting_privacy"  # privacy-service down: fail closed, retry later
    WAITING_LLM = "waiting_llm"  # local model down or paused: "AI paused"
    WAITING_APPROVAL = "waiting_approval"
    RESUMING = "resuming"  # a decision arrived; the worker continues the graph
    IGNORED = "ignored"  # not a support request (newsletter, personal mail, ...)
    APPROVED = "approved"  # ticket approved; jira-connector is creating it
    REJECTED = "rejected"
    TICKET_CREATED = "ticket_created"  # final when no allow-listed fix was found (or a report was linked)
    WAITING_OPS = "waiting_ops"  # ops-service unavailable: no evidence, retry later
    FIX_APPROVED = "fix_approved"  # T2 approved; ops-service is running the action
    FIX_REJECTED = "fix_rejected"
    FIX_FAILED = "fix_failed"  # the action failed or the probes still fail afterwards: on-call
    RESOLVED = "resolved"  # the action ran and the health re-check passed
    MESSAGE_APPROVED = "message_approved"  # T3 approved; mail-service is sending it
    WAITING_CUSTOMER = "waiting_customer"  # final for the run: a follow-up question went out
    FAILED = "failed"


# The worker picks these up (when due).
ACTIONABLE = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RESUMING,
        RunStatus.WAITING_LLM,
        RunStatus.WAITING_PRIVACY,
        RunStatus.WAITING_OPS,
    }
)
# Not finished: their people still need the vault (a reply may yet go out). Retention must not touch them.
OPEN_STATUSES = ACTIONABLE | {
    RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL,
    RunStatus.APPROVED,
    RunStatus.FIX_APPROVED,
    RunStatus.MESSAGE_APPROVED,
}
# Where the graph waits for another service's result, and the status the run shows meanwhile.
AWAITS = {
    "ticket": ("await_ticket", RunStatus.APPROVED),
    "fix": ("await_fix", RunStatus.FIX_APPROVED),
    "send": ("await_send", RunStatus.MESSAGE_APPROVED),
}


@dataclass(slots=True)
class Run:
    id: str
    email_id: str
    thread_id: str  # the mail thread
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    due_at: datetime
    node: str | None = None
    detail: str | None = None
    approval_id: str | None = None
    jira_key: str | None = None
    decision: dict[str, Any] | None = field(default=None)
    attempts: int = 0
    # Results of other services (jira-connector's ticket, ops-service's action), kept even when they arrive
    # before the graph reaches the node that waits for them.
    results: dict[str, Any] = field(default_factory=dict)

    def start(self, now: datetime) -> None:
        self.status, self.updated_at, self.detail = RunStatus.RUNNING, now, None

    def park(self, status: RunStatus, now: datetime, wait: timedelta, detail: str) -> None:
        self.status, self.updated_at, self.due_at, self.detail = status, now, now + wait, detail

    def waiting_for_approval(self, now: datetime, approval_id: str, node: str = "gate_ticket") -> None:
        self.status, self.updated_at, self.approval_id = RunStatus.WAITING_APPROVAL, now, approval_id
        self.node, self.detail = node, None

    def waiting_for(self, kind: str, now: datetime, detail: str) -> None:
        """The graph waits for another service's result. If the result is already here, continue at once."""
        node, status = AWAITS[kind]
        self.node, self.updated_at = node, now
        if kind in self.results:
            self.status, self.due_at, self.decision, self.detail = (
                RunStatus.RESUMING,
                now,
                self.results[kind],
                None,
            )
        else:
            self.status, self.detail = status, detail

    def result_arrived(self, kind: str, now: datetime, value: dict[str, Any]) -> bool:
        """Keep the result; True when the run was waiting for it (then the worker continues)."""
        if kind in self.results:
            return False  # a repeat
        self.results = {**self.results, kind: value}
        node, status = AWAITS[kind]
        if self.status is status and self.node == node:
            self.status, self.updated_at, self.due_at, self.decision = RunStatus.RESUMING, now, now, value
            return True
        return False

    def decided(self, now: datetime, decision: dict[str, Any]) -> bool:
        """A decision for the gate this run waits at; False if it is not waiting (repeat or stale)."""
        if self.status is not RunStatus.WAITING_APPROVAL:
            return False
        self.status, self.updated_at, self.due_at, self.decision = RunStatus.RESUMING, now, now, decision
        return True

    def finish(self, status: RunStatus, now: datetime, detail: str | None = None) -> None:
        self.status, self.updated_at, self.detail = status, now, detail

    def error(self, now: datetime, detail: str) -> None:
        """An unexpected failure: retry a few times, then stop and say why."""
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self.finish(RunStatus.FAILED, now, detail)
        else:
            self.park(RunStatus.QUEUED, now, timedelta(minutes=self.attempts), detail)

    def ticket(self, now: datetime, key: str) -> None:
        self.status, self.updated_at, self.jira_key, self.detail = RunStatus.TICKET_CREATED, now, key, None
