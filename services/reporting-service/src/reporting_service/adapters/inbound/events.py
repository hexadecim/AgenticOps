"""Consumes every event (`#`), once per event: the projection runs inside the inbox transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opspilot_common.events import EventEnvelope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...application import ProjectEvent
from ..outbound.sql import ReportingTables, SqlUnitOfWork


def on_any_event(
    sessions: async_sessionmaker[AsyncSession], tables: ReportingTables
) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(session: AsyncSession, envelope: EventEnvelope) -> None:
        await ProjectEvent(lambda: SqlUnitOfWork(sessions, tables, session))(envelope)

    return handle
