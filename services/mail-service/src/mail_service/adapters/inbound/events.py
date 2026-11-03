"""Event consumers. Each runs once per event (inbox table) and only translates + delegates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import SettingsChanged
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import ApplyMailboxSettings


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
