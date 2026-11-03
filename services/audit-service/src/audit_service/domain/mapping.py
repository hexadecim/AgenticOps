"""How each catalogue event reads in the audit log. Add an event: add a describer here (CLAUDE.md recipe).

Unknown or malformed events are still recorded (as `system`): the log must never silently drop anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import opspilot_common.events.catalogue as c
from opspilot_common.auth.roles import ROLE_LABELS, Role
from opspilot_common.events.envelope import EventEnvelope, EventPayload
from pydantic import ValidationError

from .entry import AuditEntry, Kind


@dataclass(frozen=True, slots=True)
class Description:
    kind: Kind
    actor: str
    event: str
    detail: str
    privacy: str
    ticket: str | None = None
    thread_id: str | None = None


def role_label(role: str | None, fallback: str = "Signed-in user") -> str:
    try:
        return ROLE_LABELS[Role(role)] if role else fallback
    except ValueError:
        return fallback


def _short(thread_id: str | None) -> str:
    return f"thread {thread_id[:8]}" if thread_id else "no thread"


_GATE = {c.Gate.TICKET: "ticket", c.Gate.FIX: "fix", c.Gate.MESSAGE: "message"}
_SECTIONS = {"mailbox": "Mailbox", "jira": "Jira", "llm": "Local AI model", "ops": "Ops"}


def _email_received(p: c.EmailReceived, e: EventEnvelope) -> Description:
    return Description(
        Kind.SYSTEM,
        "Mailbox",
        "receive_email",
        f"New email in {_short(p.thread_id)} via {p.provider.upper()}",
        "Raw email kept 30 d, not shown here",
        thread_id=p.thread_id,
    )


def _send_requested(p: c.MessageSendRequested, e: EventEnvelope) -> Description:
    to = ", ".join(f"[{t}]" for t in p.recipient_tokens) or "no recipients"
    return Description(
        Kind.TOOL,
        "Comms",
        "request_send",
        f"Approved message to {to} queued for sending",
        "Tokens only",
        p.jira_key,
        p.thread_id,
    )


def _sent(p: c.MessageSent, e: EventEnvelope) -> Description:
    to = ", ".join(f"[{t}]" for t in p.recipient_tokens) or "no recipients"
    return Description(
        Kind.TOOL,
        "Mail service",
        "send_email",
        f"Email sent to {to}",
        "Detokenised at send time only",
        p.jira_key,
        p.thread_id,
    )


def _run_status(p: c.RunStatusChanged, e: EventEnvelope) -> Description:
    where = f" at {p.node}" if p.node else ""
    detail = f"Run {p.status.replace('_', ' ')}{where}" + (f": {p.detail}" if p.detail else "")
    return Description(
        Kind.TOOL, "Orchestrator", f"run_{p.status}", detail, "No personal data", p.jira_key, p.thread_id
    )


def _approval_requested(p: c.ApprovalRequested, e: EventEnvelope) -> Description:
    return Description(
        Kind.TOOL,
        e.actor or "Orchestrator",
        f"request_{_GATE[p.gate]}_approval",
        p.title,
        "Pseudonymised input",
        p.jira_key,
        p.thread_id,
    )


def _approval_decided(p: c.ApprovalDecided, e: EventEnvelope) -> Description:
    verb = {c.Decision.APPROVE: "approve", c.Decision.REJECT: "reject", c.Decision.SEND_BACK: "send_back"}[
        p.decision
    ]
    done = {"approve": "Approved", "reject": "Rejected", "send_back": "Sent back"}[verb]
    detail = f"{done} {_GATE[p.gate]}" + (f": {p.reason}" if p.reason else "")
    if p.edits:
        changes = [f"severity → {p.edits['severity']}" if k == "severity" else k for k in sorted(p.edits)]
        detail += f" (changed: {', '.join(changes)})"
    return Description(
        Kind.HUMAN,
        role_label(p.decided_by_role),
        f"{verb}_{_GATE[p.gate]}",
        detail,
        "Approver id stored",
        thread_id=p.thread_id,
    )


def _reminder(p: c.ApprovalReminder, e: EventEnvelope) -> Description:
    minutes = p.waiting_seconds // 60
    return Description(
        Kind.SYSTEM,
        "Approval service",
        "remind_approver",
        f"{_GATE[p.gate].capitalize()} approval waiting {minutes} min",
        "No personal data",
    )


def _issue_created(p: c.JiraIssueCreated, e: EventEnvelope) -> Description:
    sev = f", {p.severity}" if p.severity else ""
    return Description(
        Kind.TOOL,
        "Jira connector",
        "create_jira_issue",
        f"Incident {p.issue_key} created in {p.project}{sev}",
        "Token and site ID only",
        p.issue_key,
        p.thread_id,
    )


def _issue_linked(p: c.JiraIssueLinked, e: EventEnvelope) -> Description:
    who = f"reporter [{p.reporter_token}]" if p.reporter_token else "this thread"
    site = f" ({p.site})" if p.site else ""
    return Description(
        Kind.TOOL,
        "Triage",
        "link_jira_issues",
        f"Linked {who}{site} to {p.issue_key}",
        "Token and site ID only",
        p.issue_key,
        p.thread_id,
    )


def _issue_failed(p: c.JiraIssueFailed, e: EventEnvelope) -> Description:
    return Description(
        Kind.TOOL,
        "Jira connector",
        "create_jira_issue_failed",
        f"Jira refused the ticket: {p.reason}",
        "Token and site ID only",
        thread_id=p.thread_id,
    )


def _probe(p: c.ProbeStatus, e: EventEnvelope) -> Description:
    latency = f" in {p.latency_ms} ms" if p.latency_ms is not None else ""
    return Description(
        Kind.SYSTEM,
        "Health",
        "probe",
        f"{p.probe}: {'healthy' if p.ok else 'failing'}{latency}. {p.detail}",
        "No personal data",
    )


def _action(p: c.ActionExecuted, e: EventEnvelope) -> Description:
    params = ", ".join(str(v) for v in p.params.values())
    return Description(
        Kind.TOOL,
        "Ops executor",
        "run_action",
        f"{p.action}({params}): {'done' if p.ok else 'failed'}. {p.detail}",
        "No personal data",
    )


def _privacy(p: c.PrivacyRecorded, e: EventEnvelope) -> Description:
    if p.actor_role:
        actor = role_label(p.actor_role)
    elif p.action == "pseudonymise":
        actor = "Privacy gate"
    else:
        actor = {"mail-service": "Mail service", "privacy-service": "Privacy service"}.get(
            e.actor or "", "Privacy service"
        )
    record = f"Token map {p.record_id} · EU" if p.record_id else (p.legal_basis or "Privacy record")
    if p.record_id and p.legal_basis:
        record = f"{record} · {p.legal_basis}"
    return Description(Kind.PRIVACY, actor, p.action, p.detail, record, thread_id=p.thread_id)


def _settings(p: c.SettingsChanged, e: EventEnvelope) -> Description:
    fields = ", ".join(p.changed_fields) or "no fields"
    return Description(
        Kind.HUMAN,
        "Admin",
        "change_settings",
        f"{_SECTIONS.get(p.section, p.section)} settings saved (v{p.version}): {fields}",
        "Field names only, no values",
    )


_USER_ACTIONS = {
    "signed_in": "Signed in",
    "signed_out": "Signed out",
    "sign_in_failed": "Sign-in failed",
    "account_locked": "Account locked after repeated failures",
    "mfa_enrolled": "Authenticator set up",
    "password_changed": "Password changed",
    "user_created": "User created",
    "user_updated": "User changed",
    "mfa_reset": "Authenticator reset",
    "password_reset": "Temporary password issued",
}


def _user(p: c.UserActivity, e: EventEnvelope) -> Description:
    text = _USER_ACTIONS.get(p.action, p.action.replace("_", " ").capitalize())
    own = e.actor == p.user_id
    subject = "" if own else f" for user {p.user_id[:8]}"
    detail = f"{text}{subject}" + (f" ({p.detail})" if p.detail else "")
    actor = "System" if e.actor == "system" else role_label(p.actor_role)
    return Description(Kind.HUMAN, actor, p.action, detail, "User id only")


def _breaker(p: c.BreakerStateChanged, e: EventEnvelope) -> Description:
    return Description(
        Kind.SYSTEM,
        p.service,
        f"circuit_{p.new}",
        f"Calls from {p.service} to {p.dependency}: {p.old} → {p.new}",
        "No personal data",
    )


def _tool(p: c.AgentToolCalled, e: EventEnvelope) -> Description:
    return Description(Kind.TOOL, p.agent, p.tool, p.detail, p.privacy, p.jira_key, p.thread_id)


_Describer = Callable[[EventPayload, EventEnvelope], Description]
DESCRIBERS: dict[str, _Describer] = {
    c.EventType.EMAIL_RECEIVED: _email_received,  # type: ignore[dict-item]
    c.EventType.MESSAGE_SEND_REQUESTED: _send_requested,  # type: ignore[dict-item]
    c.EventType.MESSAGE_SENT: _sent,  # type: ignore[dict-item]
    c.EventType.RUN_STATUS_CHANGED: _run_status,  # type: ignore[dict-item]
    c.EventType.APPROVAL_REQUESTED: _approval_requested,  # type: ignore[dict-item]
    c.EventType.APPROVAL_DECIDED: _approval_decided,  # type: ignore[dict-item]
    c.EventType.APPROVAL_REMINDER: _reminder,  # type: ignore[dict-item]
    c.EventType.JIRA_ISSUE_CREATED: _issue_created,  # type: ignore[dict-item]
    c.EventType.JIRA_ISSUE_LINKED: _issue_linked,  # type: ignore[dict-item]
    c.EventType.JIRA_ISSUE_FAILED: _issue_failed,  # type: ignore[dict-item]
    c.EventType.PROBE_STATUS: _probe,  # type: ignore[dict-item]
    c.EventType.ACTION_EXECUTED: _action,  # type: ignore[dict-item]
    c.EventType.PRIVACY_RECORDED: _privacy,  # type: ignore[dict-item]
    c.EventType.SETTINGS_CHANGED: _settings,  # type: ignore[dict-item]
    c.EventType.USER_ACTIVITY: _user,  # type: ignore[dict-item]
    c.EventType.BREAKER_STATE_CHANGED: _breaker,  # type: ignore[dict-item]
    c.EventType.AGENT_TOOL_CALLED: _tool,  # type: ignore[dict-item]
}


def describe(envelope: EventEnvelope) -> AuditEntry:
    try:
        payload = c.parse_payload(envelope)
        d = DESCRIBERS[envelope.type](payload, envelope)
    except (KeyError, ValidationError) as exc:
        reason = "unknown event type" if isinstance(exc, KeyError) else "payload did not match the catalogue"
        d = Description(
            Kind.SYSTEM,
            envelope.source,
            envelope.type.replace(".", "_"),
            f"Recorded as received ({reason})",
            "Unverified payload",
        )
    ids = envelope.payload
    return AuditEntry(
        id=envelope.id,
        occurred_at=envelope.occurred_at,
        kind=d.kind,
        actor=d.actor,
        event=d.event,
        detail=d.detail[:2000],
        privacy=d.privacy,
        event_type=envelope.type,
        source=envelope.source,
        actor_id=envelope.actor,
        ticket=d.ticket,
        thread_id=d.thread_id or (str(ids["thread_id"]) if isinstance(ids.get("thread_id"), str) else None),
        correlation_id=envelope.correlation_id,
        payload=dict(envelope.payload),
    )
