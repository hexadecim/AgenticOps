"""Event catalogue: every event type and its payload schema (v1).

Rules: add optional fields freely; anything breaking gets a new payload class with EVENT_VERSION + 1.
Payloads never carry raw personal data — only tokens such as `[PERSON_1]` and internal ids.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .envelope import EventEnvelope, EventPayload


class EventType(StrEnum):
    EMAIL_RECEIVED = "email.received"
    MESSAGE_SEND_REQUESTED = "message.send.requested"
    MESSAGE_SENT = "message.sent"
    MESSAGE_FAILED = "message.failed"
    RUN_STATUS_CHANGED = "run.status_changed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_REMINDER = "approval.reminder"
    JIRA_ISSUE_CREATED = "jira.issue.created"
    JIRA_ISSUE_LINKED = "jira.issue.linked"
    JIRA_ISSUE_FAILED = "jira.issue.failed"
    PROBE_STATUS = "probe.status"
    ACTION_EXECUTED = "action.executed"
    PRIVACY_RECORDED = "privacy.recorded"
    SETTINGS_CHANGED = "settings.changed"
    USER_ACTIVITY = "user.activity"
    BREAKER_STATE_CHANGED = "breaker.state_changed"
    AGENT_TOOL_CALLED = "agent.tool_called"


class Gate(StrEnum):
    TICKET = "ticket"  # T1: create a Jira incident
    FIX = "fix"  # T2: run a production action
    MESSAGE = "message"  # T3: send an external email


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    SEND_BACK = "send_back"  # T3 only: return draft to Comms


class EmailReceived(EventPayload):
    EVENT_TYPE = EventType.EMAIL_RECEIVED
    email_id: str
    thread_id: str
    provider: str
    received_at: datetime


class MessageRecipient(BaseModel):
    """One reporter of a message. Each gets their own email, as a reply to the email they sent."""

    email_token: str
    name_token: str | None = None
    email_id: str


class MessageSendRequested(EventPayload):
    EVENT_TYPE = EventType.MESSAGE_SEND_REQUESTED
    message_id: str
    run_id: str
    thread_id: str
    approval_id: str
    recipient_tokens: list[str]
    subject: str
    body: str = Field(description="Tokenised body; mail-service detokenises at send time")
    jira_key: str | None = None
    recipients: list[MessageRecipient] = Field(default_factory=list)
    kind: str | None = Field(default=None, description="acknowledgement | follow_up | resolution")


class MessageSent(EventPayload):
    EVENT_TYPE = EventType.MESSAGE_SENT
    message_id: str
    thread_id: str
    recipient_tokens: list[str]
    jira_key: str | None = None
    run_id: str | None = None
    approval_id: str | None = None
    kind: str | None = None
    failed_tokens: list[str] = Field(default_factory=list, description="Recipients it could not reach")
    detail: str | None = None


class MessageFailed(EventPayload):
    """An approved message reached none of its recipients; a person has to act."""

    EVENT_TYPE = EventType.MESSAGE_FAILED
    message_id: str
    run_id: str
    thread_id: str
    approval_id: str
    failed_tokens: list[str]
    reason: str
    jira_key: str | None = None
    kind: str | None = None


class RunStatusChanged(EventPayload):
    EVENT_TYPE = EventType.RUN_STATUS_CHANGED
    run_id: str
    thread_id: str
    status: (
        str  # queued | running | waiting_llm | waiting_approval | approved | ticket_created | ignored | ...
    )
    node: str | None = None
    detail: str | None = None
    jira_key: str | None = None
    email_id: str | None = None


class ApprovalRequested(EventPayload):
    EVENT_TYPE = EventType.APPROVAL_REQUESTED
    approval_id: str
    run_id: str
    thread_id: str
    gate: Gate
    title: str
    detail: str
    site: str | None = None
    severity: str | None = None
    jira_key: str | None = None
    proposal: dict[str, Any] = Field(
        default_factory=dict, description="Gate-specific data shown to the approver"
    )


class ApprovalDecided(EventPayload):
    EVENT_TYPE = EventType.APPROVAL_DECIDED
    approval_id: str
    run_id: str
    thread_id: str
    gate: Gate
    decision: Decision
    decided_by: str
    decided_by_role: str
    reason: str | None = None
    edits: dict[str, Any] = Field(default_factory=dict)


class ApprovalReminder(EventPayload):
    EVENT_TYPE = EventType.APPROVAL_REMINDER
    approval_id: str
    gate: Gate
    waiting_seconds: int
    run_id: str | None = None
    thread_id: str | None = None
    jira_key: str | None = None
    title: str | None = None
    reminder: int = Field(default=1, description="1 for the first reminder, 2 for the next, ...")


class JiraIssueCreated(EventPayload):
    EVENT_TYPE = EventType.JIRA_ISSUE_CREATED
    approval_id: str
    thread_id: str
    issue_key: str
    project: str
    severity: str | None = None
    run_id: str | None = None


class JiraIssueLinked(EventPayload):
    EVENT_TYPE = EventType.JIRA_ISSUE_LINKED
    thread_id: str
    issue_key: str
    reporter_token: str | None = None
    site: str | None = None
    approval_id: str | None = None
    run_id: str | None = None


class JiraIssueFailed(EventPayload):
    """Jira refused the change for good (e.g. unknown project or field); a person has to act."""

    EVENT_TYPE = EventType.JIRA_ISSUE_FAILED
    approval_id: str
    thread_id: str
    reason: str
    run_id: str | None = None


class ProbeStatus(EventPayload):
    """A health probe changed state (healthy ↔ failing). Repeats of the same state are not published."""

    EVENT_TYPE = EventType.PROBE_STATUS
    probe: str
    ok: bool
    detail: str
    latency_ms: int | None = None
    region: str | None = None
    run_id: str | None = None
    thread_id: str | None = None


class ActionExecuted(EventPayload):
    """An approved production action ran (or was refused at execution time: ok=False)."""

    EVENT_TYPE = EventType.ACTION_EXECUTED
    approval_id: str
    action: str
    params: dict[str, Any]
    ok: bool
    detail: str
    run_id: str | None = None
    thread_id: str | None = None
    jira_key: str | None = None
    executor: str | None = Field(default=None, description="mock | ... (which executor ran it)")


class PrivacyRecorded(EventPayload):
    EVENT_TYPE = EventType.PRIVACY_RECORDED
    action: str  # pseudonymise | reveal_identity | dsr_export | dsr_erasure | retention_purge
    subject_token: str | None = None
    detail: str
    legal_basis: str | None = None
    thread_id: str | None = None
    record_id: str | None = Field(default=None, description="Privacy record, e.g. the token map vlt-…")
    email_id: str | None = None
    actor_role: str | None = Field(default=None, description="Role of the person who acted, never a name")


class SettingsChanged(EventPayload):
    EVENT_TYPE = EventType.SETTINGS_CHANGED
    section: str  # mailbox | jira | llm | ops
    version: int
    changed_fields: list[str] = Field(default_factory=list, description="Names only, never values")


class UserActivity(EventPayload):
    EVENT_TYPE = EventType.USER_ACTIVITY
    user_id: str
    action: str  # signed_in | signed_out | mfa_failed | role_changed
    detail: str | None = None
    actor_role: str | None = Field(default=None, description="Role of the person who acted, never a name")


class BreakerStateChanged(EventPayload):
    EVENT_TYPE = EventType.BREAKER_STATE_CHANGED
    service: str
    dependency: str
    old: str
    new: str


class AgentToolCalled(EventPayload):
    EVENT_TYPE = EventType.AGENT_TOOL_CALLED
    run_id: str
    thread_id: str
    agent: str  # Intake | Triage | Diagnostics | Comms | Health
    tool: str
    detail: str
    privacy: str = "Pseudonymised input"
    jira_key: str | None = None


PAYLOADS: dict[str, type[EventPayload]] = {
    cls.EVENT_TYPE: cls
    for cls in (
        EmailReceived,
        MessageSendRequested,
        MessageSent,
        MessageFailed,
        RunStatusChanged,
        ApprovalRequested,
        ApprovalDecided,
        ApprovalReminder,
        JiraIssueCreated,
        JiraIssueLinked,
        JiraIssueFailed,
        ProbeStatus,
        ActionExecuted,
        PrivacyRecorded,
        SettingsChanged,
        UserActivity,
        BreakerStateChanged,
        AgentToolCalled,
    )
}


def parse_payload(envelope: EventEnvelope) -> EventPayload:
    """Validate an envelope's payload against the catalogue. Unknown types raise KeyError."""
    return PAYLOADS[envelope.type].model_validate(envelope.payload)
