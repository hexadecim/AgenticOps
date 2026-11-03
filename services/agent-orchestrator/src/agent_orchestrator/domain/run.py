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
    TICKET_CREATED = "ticket_created"
    FAILED = "failed"


# The worker picks these up (when due).
ACTIONABLE = frozenset(
    {RunStatus.QUEUED, RunStatus.RESUMING, RunStatus.WAITING_LLM, RunStatus.WAITING_PRIVACY}
)


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

    def start(self, now: datetime) -> None:
        self.status, self.updated_at, self.detail = RunStatus.RUNNING, now, None

    def park(self, status: RunStatus, now: datetime, wait: timedelta, detail: str) -> None:
        self.status, self.updated_at, self.due_at, self.detail = status, now, now + wait, detail

    def waiting_for_approval(self, now: datetime, approval_id: str) -> None:
        self.status, self.updated_at, self.approval_id = RunStatus.WAITING_APPROVAL, now, approval_id
        self.node, self.detail = "gate_ticket", None

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
