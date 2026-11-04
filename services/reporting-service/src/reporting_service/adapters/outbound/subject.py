"""A person's cases on the dashboard: counts, timings and tokenised titles."""

from __future__ import annotations

from opspilot_common.events import Outbox
from opspilot_common.events.catalogue import SubjectErasureRequested
from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import erased_envelope
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sql import ReportingTables

SERVICE = "reporting-service"


class SqlReportingSubjectStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: ReportingTables) -> None:
        self._sessions, self._t = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def report(self, query: SubjectQuery) -> StoreReport:
        cases, timeline = self._t.cases, self._t.timeline
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(cases.c.title, cases.c.status, cases.c.jira_key).where(
                        cases.c.run_id.in_(query.run_ids)
                    )
                )
            )
            notes = (
                await session.execute(select(func.count()).where(timeline.c.run_id.in_(query.run_ids)))
            ).scalar_one()
        return StoreReport(
            store="reporting",
            label="Dashboard",
            what="cases and timeline notes",
            count=len(rows) + int(notes),
            items=[{"title": r.title, "status": r.status, "ticket": r.jira_key} for r in rows]
            if query.include_items
            else [],
        )

    async def erase(self, request: SubjectErasureRequested) -> None:
        t = self._t
        async with self._sessions() as session, session.begin():
            cases = await session.execute(delete(t.cases).where(t.cases.c.run_id.in_(request.run_ids)))
            notes = await session.execute(delete(t.timeline).where(t.timeline.c.run_id.in_(request.run_ids)))
            await session.execute(delete(t.emails).where(t.emails.c.email_id.in_(request.own_email_ids)))
            count = int(cases.rowcount) + int(notes.rowcount)  # type: ignore[attr-defined]
            await self._outbox.add(session, erased_envelope(SERVICE, request, "reporting", count))
