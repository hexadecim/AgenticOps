"""Consumes every event (`#`). Runs inside the inbox transaction, so an event is recorded exactly once."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope
from sqlalchemy.ext.asyncio import AsyncSession

from ...application import RecordEvent
from ..outbound.sql import AuditTables, SqlAuditLog


def on_any_event(
    record: RecordEvent, tables: AuditTables
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(session: AsyncSession, envelope: EventEnvelope) -> None:
        await record(envelope, SqlAuditLog(session, tables))

    return handle
