"""Event consumers. Each runs once per event (inbox table) and only translates + delegates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    DsrSendRequested,
    MessageSendRequested,
    SettingsChanged,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import ApplyMailboxSettings, QueueDsrMessage, QueueMessage, RecordMessageApproval

MESSAGE_BINDINGS = ["message.send.requested", "approval.decided", "dsr.send_requested"]


def on_settings_changed(
    apply: ApplyMailboxSettings, after_change: Callable[[], None]
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    """Mailbox settings changed → rebuild the adapter and poll right away. Raising lets RabbitMQ retry
    (e.g. settings-service briefly down)."""

    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, SettingsChanged) and payload.section == "mailbox" and await apply():
            after_change()

    return handle


def on_message_event(
    queue: QueueMessage,
    approval: RecordMessageApproval,
    wake: Callable[[], None],
    dsr: QueueDsrMessage | None = None,
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    """The request and the decision arrive in any order; the sender needs both."""

    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, MessageSendRequested):
            if await queue(payload):
                wake()
        elif (isinstance(payload, ApprovalDecided) and await approval(payload)) or (
            isinstance(payload, DsrSendRequested) and dsr is not None and await dsr(payload)
        ):
            wake()

    return handle
