"""Event consumers (inbox-guarded): the stores' acknowledgements and how a data-request email went."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope, parse_payload
from opspilot_common.events.catalogue import DsrMessageResult, SubjectStoreErased
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import RecordDsrMessage, RecordStoreErased

BINDINGS = ["subject.store_erased", "dsr.message_result"]


def on_event(
    erased: RecordStoreErased, message: RecordDsrMessage
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, SubjectStoreErased):
            await erased(payload)
        elif isinstance(payload, DsrMessageResult):
            await message(payload)

    return handle
