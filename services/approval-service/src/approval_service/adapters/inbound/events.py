"""Event consumers: translate and delegate, once per event (inbox)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import (
    ActionExecuted,
    ApprovalRequested,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
    MessageFailed,
    MessageSent,
    RunStatusChanged,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import (
    RecordFixHealth,
    RecordFixOutcome,
    RecordMessageOutcome,
    RecordRequest,
    RecordTicketOutcome,
)

BINDINGS = [
    "approval.requested",
    "jira.issue.created",
    "jira.issue.linked",
    "jira.issue.failed",
    "action.executed",
    "run.status_changed",
    "message.sent",
    "message.failed",
]


def on_event(
    record_request: RecordRequest,
    record_outcome: RecordTicketOutcome,
    record_fix: RecordFixOutcome,
    record_health: RecordFixHealth,
    record_message: RecordMessageOutcome | None = None,
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, ApprovalRequested):
            await record_request(payload, envelope.occurred_at)
        elif isinstance(payload, JiraIssueCreated | JiraIssueLinked | JiraIssueFailed):
            await record_outcome(payload)
        elif isinstance(payload, ActionExecuted):
            await record_fix(payload)
        elif isinstance(payload, RunStatusChanged):
            await record_health(payload)
        elif isinstance(payload, MessageSent | MessageFailed) and record_message is not None:
            await record_message(payload)

    return handle
