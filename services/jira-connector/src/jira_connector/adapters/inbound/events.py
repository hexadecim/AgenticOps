"""Event consumers (inbox-guarded): proposals, approvals and settings changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import ApprovalDecided, ApprovalRequested, SettingsChanged
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import ActiveTracker, QueueJob, StoreProposal

BINDINGS = ["approval.requested", "approval.decided", "settings.changed"]


def on_event(
    store: StoreProposal, queue: QueueJob, active: ActiveTracker, wake: Callable[[], None]
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, ApprovalRequested):
            await store(payload)
        elif isinstance(payload, ApprovalDecided):
            await queue(payload)
            wake()
        elif isinstance(payload, SettingsChanged) and payload.section == "jira":
            await active.get(refresh=True)
            wake()

    return handle
