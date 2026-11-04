"""The link between a person's runs and Jira. Issues themselves hold tokens only and stay in Jira: with the
vault entry gone, those tokens no longer lead to anyone.
"""

from __future__ import annotations

from opspilot_common.events import Outbox
from opspilot_common.events.catalogue import SubjectErasureRequested
from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import erased_envelope
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sql import JiraTables

SERVICE = "jira-connector"


class SqlJiraSubjectStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: JiraTables) -> None:
        self._sessions, self._p, self._j = sessions, tables.proposals, tables.jobs
        self._outbox = Outbox(tables.outbox)

    async def report(self, query: SubjectQuery) -> StoreReport:
        async with self._sessions() as session:
            jobs = list(
                await session.execute(
                    select(self._j.c.action, self._j.c.status, self._j.c.issue_key).where(
                        self._j.c.run_id.in_(query.run_ids)
                    )
                )
            )
            proposals = (
                await session.execute(select(func.count()).where(self._p.c.run_id.in_(query.run_ids)))
            ).scalar_one()
        return StoreReport(
            store="jira",
            label="Jira fields",
            what="reporter token on tickets",
            count=max(len(jobs), int(proposals)),
            erase_action="Unlink token",
            items=[{"action": r.action, "status": r.status, "ticket": r.issue_key} for r in jobs]
            if query.include_items
            else [],
        )

    async def erase(self, request: SubjectErasureRequested) -> None:
        async with self._sessions() as session, session.begin():
            jobs = await session.execute(delete(self._j).where(self._j.c.run_id.in_(request.run_ids)))
            proposals = await session.execute(delete(self._p).where(self._p.c.run_id.in_(request.run_ids)))
            count = max(int(jobs.rowcount), int(proposals.rowcount))  # type: ignore[attr-defined]
            detail = "Tickets stay in Jira with a token that no longer leads to anyone."
            await self._outbox.add(session, erased_envelope(SERVICE, request, "jira", count, detail))
