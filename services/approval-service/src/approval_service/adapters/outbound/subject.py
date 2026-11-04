"""Approvals on a person's runs (tokenised proposals and decisions). The audit log keeps the record of each
decision.
"""

from __future__ import annotations

from opspilot_common.events import Outbox
from opspilot_common.events.catalogue import SubjectErasureRequested
from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import erased_envelope
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sql import ApprovalTables

SERVICE = "approval-service"


class SqlApprovalSubjectStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: ApprovalTables) -> None:
        self._sessions, self._t = sessions, tables.approvals
        self._outbox = Outbox(tables.outbox)

    async def report(self, query: SubjectQuery) -> StoreReport:
        t = self._t
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(t.c.gate, t.c.status, t.c.title, t.c.detail, t.c.requested_at).where(
                        t.c.run_id.in_(query.run_ids)
                    )
                )
            )
        return StoreReport(
            store="approval",
            label="Approvals",
            what="proposals and decisions on their case",
            count=len(rows),
            items=[
                {
                    "gate": r.gate,
                    "status": r.status,
                    "title": r.title,
                    "detail": r.detail,
                    "requested_at": r.requested_at.isoformat(),
                }
                for r in rows
            ]
            if query.include_items
            else [],
        )

    async def erase(self, request: SubjectErasureRequested) -> None:
        async with self._sessions() as session, session.begin():
            done = await session.execute(delete(self._t).where(self._t.c.run_id.in_(request.run_ids)))
            count = int(done.rowcount)  # type: ignore[attr-defined]
            await self._outbox.add(session, erased_envelope(SERVICE, request, "approval", count))
