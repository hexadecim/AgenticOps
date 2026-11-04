"""Read-only: what the audit log says about a person. Nothing here changes an entry. It keeps them (legal
obligation) with tokens only, which lead nowhere once the vault entry is gone.
"""

from __future__ import annotations

from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import mentions
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sql import AuditTables


class SqlAuditSubjectReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: AuditTables) -> None:
        self._sessions, self._t = sessions, tables.entries

    async def report(self, query: SubjectQuery) -> StoreReport:
        t = self._t
        keys = [*query.tokens, *query.own_email_ids, *query.run_ids]
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(t.c.occurred_at, t.c.actor, t.c.event, t.c.detail, t.c.ticket)
                    .where(or_(mentions(t.c.detail, keys), mentions(t.c.payload, keys)))
                    .order_by(t.c.occurred_at)
                )
            )
        return StoreReport(
            store="audit",
            label="Audit log",
            what="decisions about this person",
            count=len(rows),
            erase_action="Keep, token only",
            items=[
                {
                    "at": r.occurred_at.isoformat(),
                    "actor": r.actor,
                    "event": r.event,
                    "detail": r.detail,
                    "ticket": r.ticket,
                }
                for r in rows
            ]
            if query.include_items
            else [],
        )
