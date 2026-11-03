"""Event consumers (inbox-guarded): fix proposals, decisions and settings changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import ApprovalDecided, ApprovalRequested, SettingsChanged
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import ActiveSettings, QueueFix, StoreFixProposal

BINDINGS = ["approval.requested", "approval.decided", "settings.changed"]


def on_event(
    store: StoreFixProposal,
    queue: QueueFix,
    settings: ActiveSettings,
    wake: Callable[[], None],
    reindex: Callable[[], None],
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, ApprovalRequested):
            await store(payload)
        elif isinstance(payload, ApprovalDecided):
            if await queue(payload):
                wake()
        elif isinstance(payload, SettingsChanged) and payload.section in ("ops", "llm"):
            await settings.get(refresh=True)
            reindex()

    return handle
