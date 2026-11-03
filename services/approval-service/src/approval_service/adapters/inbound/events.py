"""Event consumers: translate and delegate, once per event (inbox)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import (
    ApprovalRequested,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import RecordRequest, RecordTicketOutcome

BINDINGS = ["approval.requested", "jira.issue.created", "jira.issue.linked", "jira.issue.failed"]


def on_event(
    record_request: RecordRequest, record_outcome: RecordTicketOutcome
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, ApprovalRequested):
            await record_request(payload, envelope.occurred_at)
        elif isinstance(payload, JiraIssueCreated | JiraIssueLinked | JiraIssueFailed):
            await record_outcome(payload)

    return handle
