"""SQLAlchemy adapters for schema `orchestrator` (runs, outbox, inbox). LangGraph checkpoints live in the same
schema, in the tables AsyncPostgresSaver creates."""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.run import ACTIONABLE, Run, RunStatus
from ...ports import EventRecorder, RunRepository

SERVICE = "agent-orchestrator"


class OrchestratorTables:
    def __init__(self, schema: str | None = "orchestrator") -> None:
        self.metadata = MetaData(schema=schema)
        self.runs = Table(
            "runs",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("email_id", String(36), nullable=False, unique=True),
            Column("thread_id", String(36), nullable=False, index=True),
            Column("status", String(20), nullable=False, index=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            Column("due_at", DateTime(timezone=True), nullable=False, index=True),
            Column("node", String(40), nullable=True),
            Column("detail", Text, nullable=True),
            Column("approval_id", String(36), nullable=True, index=True),
            Column("jira_key", String(40), nullable=True),
            Column("decision", JSON, nullable=True),
            Column("attempts", Integer, nullable=False),
            Column("results", JSON, nullable=True),
            # Tokens and email id only. Written by the graph, so not part of the Run the worker saves.
            Column("reporter", JSON, nullable=True),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _row(r: Run) -> dict[str, Any]:
    return {
        "email_id": r.email_id,
        "thread_id": r.thread_id,
        "status": r.status.value,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "due_at": r.due_at,
        "node": r.node,
        "detail": r.detail,
        "approval_id": r.approval_id,
        "jira_key": r.jira_key,
        "decision": r.decision,
        "attempts": r.attempts,
        "results": r.results or None,
    }


def _run(r: Any) -> Run:
    return Run(
        id=r.id,
        email_id=r.email_id,
        thread_id=r.thread_id,
        status=RunStatus(r.status),
        created_at=_aware(r.created_at),
        updated_at=_aware(r.updated_at),
        due_at=_aware(r.due_at),
        node=r.node,
        detail=r.detail,
        approval_id=r.approval_id,
        jira_key=r.jira_key,
        decision=r.decision,
        attempts=r.attempts,
        results=dict(r.results or {}),
    )


class SqlRunRepository:
    def __init__(self, session: AsyncSession, tables: OrchestratorTables) -> None:
        self._s, self._t = session, tables.runs

    async def _one(self, *where: Any) -> Run | None:
        row = (await self._s.execute(select(self._t).where(*where))).first()
        return _run(row) if row else None

    async def get(self, run_id: str) -> Run | None:
        return await self._one(self._t.c.id == run_id)

    async def by_email(self, email_id: str) -> Run | None:
        return await self._one(self._t.c.email_id == email_id)

    async def by_approval(self, approval_id: str) -> Run | None:
        return await self._one(self._t.c.approval_id == approval_id)

    async def add(self, run: Run) -> None:
        await self._s.execute(insert(self._t).values(id=run.id, **_row(run)))

    async def save(self, run: Run) -> None:
        await self._s.execute(update(self._t).where(self._t.c.id == run.id).values(**_row(run)))

    async def due(self, now: datetime, limit: int) -> list[Run]:
        rows = await self._s.execute(
            select(self._t)
            .where(self._t.c.status.in_([s.value for s in ACTIONABLE]), self._t.c.due_at <= now)
            .order_by(self._t.c.due_at)
            .limit(limit)
        )
        return [_run(r) for r in rows]

    async def requeue_interrupted(self, now: datetime) -> int:
        result = await self._s.execute(
            update(self._t)
            .where(self._t.c.status == RunStatus.RUNNING.value)
            .values(status=RunStatus.QUEUED.value, due_at=now, updated_at=now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def counts(self, since: datetime) -> dict[RunStatus, int]:
        terminal = [
            s.value
            for s in RunStatus
            if s not in ACTIONABLE | {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}
        ]
        rows = await self._s.execute(
            select(self._t.c.status, func.count())
            .where(or_(self._t.c.status.not_in(terminal), self._t.c.updated_at >= since))
            .group_by(self._t.c.status)
        )
        return {RunStatus(status): int(n) for status, n in rows}

    async def ticket_for_thread(self, thread_id: str, *, exclude_run: str) -> str | None:
        row = (
            await self._s.execute(
                select(self._t.c.jira_key)
                .where(
                    self._t.c.thread_id == thread_id,
                    self._t.c.jira_key.is_not(None),
                    self._t.c.id != exclude_run,
                )
                .order_by(self._t.c.created_at)
                .limit(1)
            )
        ).first()
        return str(row.jira_key) if row else None


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    runs: RunRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OrchestratorTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.runs = SqlRunRepository(self._session, self._tables)
        self._recorder = _Recorder()
        self.events = self._recorder
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._session.in_transaction():
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        for envelope in self._recorder.pending:
            await self._outbox.add(self._session, envelope)
        self._recorder.pending.clear()
        await self._session.commit()
        await self._session.begin()


class SqlEventSink:
    """Graph nodes emit events through here: one outbox row, committed at once."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OrchestratorTables) -> None:
        self._sessions, self._outbox = sessions, Outbox(tables.outbox)

    async def emit(self, payload: EventPayload, *, actor: str | None = None) -> None:
        envelope = EventEnvelope.wrap(
            payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get()
        )
        async with self._sessions() as session, session.begin():
            await self._outbox.add(session, envelope)


class SqlThreadTickets:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OrchestratorTables) -> None:
        self._sessions, self._tables = sessions, tables

    async def ticket_for_thread(self, thread_id: str, *, exclude_run: str) -> str | None:
        async with self._sessions() as session:
            return await SqlRunRepository(session, self._tables).ticket_for_thread(
                thread_id, exclude_run=exclude_run
            )

    async def reporters(self, jira_key: str) -> int:
        async with self._sessions() as session:
            t = self._tables.runs
            count = (await session.execute(select(func.count()).where(t.c.jira_key == jira_key))).scalar_one()
        return max(1, int(count))

    async def remember_reporter(self, run_id: str, reporter: dict[str, Any]) -> None:
        t = self._tables.runs
        async with self._sessions() as session, session.begin():
            await session.execute(update(t).where(t.c.id == run_id).values(reporter=reporter))

    async def linked_reporters(self, jira_key: str, *, exclude_run: str) -> list[dict[str, Any] | None]:
        t = self._tables.runs
        async with self._sessions() as session:
            rows = await session.execute(
                select(t.c.reporter)
                .where(t.c.jira_key == jira_key, t.c.id != exclude_run)
                .order_by(t.c.created_at)
            )
            return [dict(r.reporter) if r.reporter else None for r in rows]
