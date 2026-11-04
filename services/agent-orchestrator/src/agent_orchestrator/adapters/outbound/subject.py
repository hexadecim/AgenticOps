"""What the orchestrator holds on a person: their runs and the graph checkpoints behind them. Tokenised, and
keyed by the emails they wrote, since another person's run must survive their erasure.
"""

from __future__ import annotations

from typing import Any

from opspilot_common.events import Outbox
from opspilot_common.events.catalogue import SubjectErasureRequested
from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import erased_envelope
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.errors import RunInProgressError
from ...domain.run import OPEN_STATUSES, RunStatus
from .sql import OrchestratorTables

SERVICE = "agent-orchestrator"


class SqlRunSubjectStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OrchestratorTables) -> None:
        self._sessions, self._t = sessions, tables.runs
        self._outbox = Outbox(tables.outbox)
        self._checkpointer: Any = None

    def use_checkpointer(self, checkpointer: Any) -> None:
        """It is opened in the app's lifespan, after this store is built."""
        self._checkpointer = checkpointer

    async def report(self, query: SubjectQuery) -> StoreReport:
        t = self._t
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(t.c.id, t.c.status, t.c.jira_key, t.c.created_at).where(
                        t.c.email_id.in_(query.own_email_ids)
                    )
                )
            )
        statuses = [RunStatus(r.status) for r in rows]
        return StoreReport(
            store="orchestrator",
            label="Thread checkpoints",
            what="agent runs on their emails",
            count=len(rows),
            run_ids=[str(r.id) for r in rows],
            open_runs=sum(s in OPEN_STATUSES for s in statuses),
            busy_runs=statuses.count(RunStatus.RUNNING),
            items=[
                {"status": r.status, "ticket": r.jira_key, "created_at": r.created_at.isoformat()}
                for r in rows
            ]
            if query.include_items
            else [],
        )

    async def erase(self, request: SubjectErasureRequested) -> None:
        t = self._t
        async with self._sessions() as session, session.begin():
            busy = await session.execute(
                select(t.c.id).where(t.c.id.in_(request.run_ids), t.c.status == RunStatus.RUNNING.value)
            )
            if busy.first() is not None:
                raise RunInProgressError()  # RabbitMQ redelivers; the step ends within minutes
            if self._checkpointer is not None:
                for run_id in request.run_ids:
                    await self._checkpointer.adelete_thread(run_id)
            done = await session.execute(delete(t).where(t.c.id.in_(request.run_ids)))
            count = int(done.rowcount)  # type: ignore[attr-defined]
            await self._outbox.add(session, erased_envelope(SERVICE, request, "orchestrator", count))
