"""Jira work as OpsPilot sees it: approved proposals become jobs; a job runs until Jira confirms it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

LABEL_PREFIX = "opspilot-"
MAX_BACKOFF = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class JiraSettings:
    version: int
    base_url: str
    email: str
    project_key: str
    issue_type: str
    api_token: str = field(repr=False)


class Action(StrEnum):
    CREATE = "create"  # a new incident
    LINK = "link"  # another report of an existing incident: comment on it


@dataclass(frozen=True, slots=True)
class TicketFields:
    summary: str
    description: str
    severity: str | None = None
    site: str | None = None
    category: str | None = None

    def with_edits(self, edits: dict[str, Any]) -> TicketFields:
        return TicketFields(
            summary=str(edits.get("summary", self.summary)),
            description=self.description,
            severity=str(edits.get("severity", self.severity))
            if edits.get("severity", self.severity)
            else None,
            site=self.site,
            category=self.category,
        )


@dataclass(frozen=True, slots=True)
class Proposal:
    """What the orchestrator proposed at the ticket gate (from `approval.requested`)."""

    approval_id: str
    run_id: str
    thread_id: str
    action: Action
    ticket: TicketFields
    link_to: str | None = None  # issue key, for Action.LINK
    reporter_token: str | None = None


class JobStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Job:
    """One approved Jira change. Its id is the approval id: the idempotency key end to end."""

    id: str
    run_id: str
    thread_id: str
    action: Action
    ticket: TicketFields
    created_at: datetime
    next_attempt_at: datetime
    link_to: str | None = None
    reporter_token: str | None = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    issue_key: str | None = None
    last_error: str | None = None
    trace: str | None = None  # W3C trace context of the case

    @property
    def label(self) -> str:
        """Put on the created issue, so a replay finds it instead of creating a second one."""
        return f"{LABEL_PREFIX}{self.id}"

    def labels(self) -> list[str]:
        extra = [self.ticket.severity, self.ticket.site, self.ticket.category]
        return ["opspilot", self.label, *(_label(v) for v in extra if v)]

    def done(self, key: str) -> None:
        self.status, self.issue_key, self.last_error = JobStatus.DONE, key, None
        self.attempts += 1

    def retry_later(self, now: datetime, reason: str, retry_after: float | None = None) -> None:
        self.attempts += 1
        backoff = min(timedelta(seconds=30 * 2 ** min(self.attempts - 1, 6)), MAX_BACKOFF)
        if retry_after:
            backoff = max(backoff, timedelta(seconds=retry_after))
        self.next_attempt_at, self.last_error = now + backoff, reason

    def wait(self, now: datetime, reason: str, delay: timedelta = timedelta(minutes=1)) -> None:
        """Not an attempt (e.g. Jira not configured yet): check again later without backing off."""
        self.next_attempt_at, self.last_error = now + delay, reason

    def fail(self, reason: str) -> None:
        self.status, self.last_error = JobStatus.FAILED, reason
        self.attempts += 1


def _label(value: str) -> str:
    """Jira labels cannot contain spaces."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-").lower()[:60]


@dataclass(frozen=True, slots=True)
class IssueSummary:
    key: str
    summary: str
    status: str
    created: str


@dataclass(frozen=True, slots=True)
class JiraContext:
    configured: bool
    project: str | None = None
    issue_type: str | None = None
    recent: list[IssueSummary] = field(default_factory=list)


def adf(text: str) -> dict[str, Any]:
    """Atlassian Document Format: Jira Cloud v3 rejects plain text descriptions and comments."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()] or [""]
    content = []
    for paragraph in paragraphs:
        lines = paragraph.split("\n")
        inline: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if i:
                inline.append({"type": "hardBreak"})
            if line:
                inline.append({"type": "text", "text": line})
        content.append({"type": "paragraph", "content": inline})
    return {"type": "doc", "version": 1, "content": content}
