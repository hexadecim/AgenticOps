"""Read models for the dashboard, built only from events (CQRS read side). Pure logic, no I/O.

Everything here is about cases, timings and dependencies; content stays tokenised (titles come from
`approval.requested`, which carries the pseudonymised subject).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class Stage(StrEnum):
    INTAKE = "intake"  # queued or being read by the model (incl. AI paused)
    DIAGNOSTICS = "diagnostics"  # from milestone 6
    APPROVAL = "approval"  # waiting at a gate
    CUSTOMER = "customer"  # waiting on the customer, from milestone 7
    ENGINEERING = "engineering"  # ticket created or being created, open in Jira
    CLOSED = "closed"  # ignored, rejected, failed


_STAGE_OF = {
    "queued": Stage.INTAKE,
    "running": Stage.INTAKE,
    "resuming": Stage.INTAKE,
    "waiting_llm": Stage.INTAKE,
    "waiting_privacy": Stage.INTAKE,
    "waiting_approval": Stage.APPROVAL,
    "approved": Stage.ENGINEERING,
    "ticket_created": Stage.ENGINEERING,
}


@dataclass(slots=True)
class Case:
    """One email's run, as the dashboard sees it."""

    run_id: str
    thread_id: str
    status: str = "queued"
    email_id: str | None = None
    received_at: datetime | None = None  # when OpsPilot received the email
    queued_at: datetime | None = None
    triaged_at: datetime | None = None
    requested_at: datetime | None = None  # approval requested
    decided_at: datetime | None = None
    decision: str | None = None
    ticket_at: datetime | None = None
    outcome: str | None = None  # created | linked | failed
    jira_key: str | None = None
    title: str | None = None
    severity: str | None = None
    site: str | None = None
    paused: bool = False  # waited for the local model at least once
    updated_at: datetime | None = None

    @property
    def stage(self) -> Stage:
        return _STAGE_OF.get(self.status, Stage.CLOSED)

    @property
    def started_at(self) -> datetime | None:
        return self.received_at or self.queued_at


@dataclass(frozen=True, slots=True)
class StageCount:
    stage: Stage
    label: str
    count: int | None  # None = not tracked yet (later milestone)
    note: str = ""


def stage_counts(cases: list[Case], *, resolved_today: int | None = None) -> list[StageCount]:
    counts = {s: 0 for s in Stage}
    paused = 0
    for case in cases:
        counts[case.stage] += 1
        paused += case.status == "waiting_llm"
    return [
        StageCount(
            Stage.INTAKE, "Intake", counts[Stage.INTAKE], f"{paused} paused for the AI" if paused else ""
        ),
        StageCount(Stage.DIAGNOSTICS, "Diagnostics", None, "from milestone 6"),
        StageCount(Stage.APPROVAL, "Waiting for approval", counts[Stage.APPROVAL]),
        StageCount(Stage.CUSTOMER, "Waiting on customer", None, "from milestone 7"),
        StageCount(Stage.ENGINEERING, "With engineering", counts[Stage.ENGINEERING], "open in Jira"),
        StageCount(Stage.CLOSED, "Resolved today", resolved_today, "needs Jira status sync"),
    ]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "\u2013"  # en dash: no measurement yet
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass(frozen=True, slots=True)
class ServiceLevel:
    id: str
    label: str
    value: str
    detail: str = ""
    breached: bool = False


TICKET_TARGET = timedelta(minutes=5)


def service_levels(cases: list[Case], *, since: datetime) -> list[ServiceLevel]:
    """Measured over cases that started after `since` (the dashboard shows the last 24 h)."""
    recent = [c for c in cases if (c.started_at or c.updated_at or since) >= since]

    def span(start: datetime | None, end: datetime | None) -> float | None:
        return (end - start).total_seconds() if start and end and end >= start else None

    to_ticket = [
        s for c in recent if c.outcome == "created" and (s := span(c.started_at, c.ticket_at)) is not None
    ]
    to_request = [s for c in recent if (s := span(c.started_at, c.requested_at)) is not None]
    approval_wait = [s for c in recent if (s := span(c.requested_at, c.decided_at)) is not None]
    ticket_median = _median(to_ticket)
    linked = sum(c.outcome == "linked" for c in recent)
    tickets = sum(c.outcome in ("created", "linked") for c in recent)
    return [
        ServiceLevel(
            "email_to_ticket",
            "Email to Jira ticket, median",
            duration_text(ticket_median),
            f"target < {int(TICKET_TARGET.total_seconds() // 60)} min · {len(to_ticket)} tickets",
            breached=ticket_median is not None and ticket_median > TICKET_TARGET.total_seconds(),
        ),
        ServiceLevel(
            "email_to_request",
            "Email to approval request (AI triage), median",
            duration_text(_median(to_request)),
            f"{len(to_request)} emails",
        ),
        ServiceLevel(
            "approval_wait",
            "Waiting for a person, median",
            duration_text(_median(approval_wait)),
            "request to decision",
        ),
        ServiceLevel(
            "linked", "Duplicates linked instead of new tickets", str(linked), f"of {tickets} reports"
        ),
        ServiceLevel(
            "ignored",
            "Emails ignored as not support",
            str(sum(c.status == "ignored" for c in recent)),
            "newsletters etc.",
        ),
        ServiceLevel(
            "needs_person",
            "Runs stopped and needing a person",
            str(n := sum(c.status == "failed" or c.outcome == "failed" for c in recent)),
            "failed triage or Jira refused",
            breached=n > 0,
        ),
        ServiceLevel(
            "ai_paused",
            "Emails that waited for the local AI",
            str(sum(c.paused for c in recent)),
            "Ollama unavailable",
        ),
    ]


class Health(StrEnum):
    UP = "up"
    DOWN = "down"
    RECOVERING = "recovering"


_HEALTH = {"closed": Health.UP, "open": Health.DOWN, "half_open": Health.RECOVERING}


@dataclass(slots=True)
class Dependency:
    """Latest circuit state per dependency, as reported by the services calling it."""

    name: str
    states: dict[str, str] = field(default_factory=dict)  # calling service → closed | open | half_open
    since: datetime | None = None

    def record(self, service: str, state: str, at: datetime) -> None:
        if at >= (self.since or at):
            self.since = at
        self.states[service] = state

    @property
    def health(self) -> Health:
        """Down if any caller has it open: one service's view is enough to warn."""
        values = [_HEALTH.get(s, Health.UP) for s in self.states.values()]
        if Health.DOWN in values:
            return Health.DOWN
        if Health.RECOVERING in values:
            return Health.RECOVERING
        return Health.UP


# Shown even before anything failed, so the panel is never empty.
KEY_DEPENDENCIES = {
    "ollama": "Local AI model",
    "mail-provider": "Mailbox",
    "jira": "Jira",
    "privacy-service": "Privacy service",
    "settings-service": "Settings service",
}
LABELS = {
    **KEY_DEPENDENCIES,
    "ollama-embed": "AI embeddings",
    "jira-connector": "Jira connector",
    "mail-service": "Mail service",
    "approval-service": "Approval service",
    "identity-service": "Sign-in service",
    "agent-orchestrator": "Agents",
    "audit-service": "Audit service",
    "reporting-service": "Reporting",
    "redis": "Redis",
    "api-gateway": "Gateway",
}


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    id: str  # event id
    at: datetime
    actor: str
    text: str
    run_id: str | None = None
    thread_id: str | None = None
    jira_key: str | None = None
    system: bool = False  # dependency changes: shown on any incident that was open at the time
