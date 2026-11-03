"""Event consumers (inbox-guarded). They only create or wake runs; the worker does the slow work."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import (
    ActionExecuted,
    ApprovalDecided,
    EmailReceived,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
    SettingsChanged,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import RecordDecision, RecordFixResult, RecordTicketResult, StartRun
from ..outbound.llm import LlmSettingsCache

BINDINGS = [
    "email.received",
    "approval.decided",
    "jira.issue.created",
    "jira.issue.linked",
    "jira.issue.failed",
    "action.executed",
    "settings.changed",
]


def on_event(
    start: StartRun,
    decide: RecordDecision,
    ticket: RecordTicketResult,
    llm_settings: LlmSettingsCache,
    wake: Callable[[], None],
    fix: RecordFixResult | None = None,
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, EmailReceived):
            if await start(payload):
                wake()
        elif isinstance(payload, ApprovalDecided):
            if await decide(payload):
                wake()
        elif isinstance(payload, JiraIssueCreated | JiraIssueLinked | JiraIssueFailed):
            if await ticket(payload):
                wake()
        elif isinstance(payload, ActionExecuted) and fix is not None:
            if await fix(payload):
                wake()
        elif isinstance(payload, SettingsChanged) and payload.section == "llm":
            await llm_settings.get(refresh=True)
            wake()

    return handle
