"""SQLAlchemy adapters for schema `jira`. A job row is the outbox for one Jira write."""

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
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.model import Action, Job, JobStatus, Proposal, TicketFields
from ...ports import EventRecorder, JobRepository, ProposalRepository

SERVICE = "jira-connector"


class JiraTables:
    def __init__(self, schema: str | None = "jira") -> None:
        self.metadata = MetaData(schema=schema)
        self.proposals = Table(
            "proposals",
            self.metadata,
            Column("approval_id", String(36), primary_key=True),
            Column("run_id", String(36), nullable=False),
            Column("thread_id", String(36), nullable=False),
            Column("action", String(10), nullable=False),
            Column("ticket", JSON, nullable=False),
            Column("link_to", String(40), nullable=True),
            Column("reporter_token", String(40), nullable=True),
        )
        self.jobs = Table(
            "jobs",
            self.metadata,
            Column("id", String(36), primary_key=True),  # = approval id (idempotency key)
            Column("run_id", String(36), nullable=False),
            Column("thread_id", String(36), nullable=False),
            Column("action", String(10), nullable=False),
            Column("ticket", JSON, nullable=False),
            Column("link_to", String(40), nullable=True),
            Column("reporter_token", String(40), nullable=True),
            Column("status", String(10), nullable=False, index=True),
            Column("attempts", Integer, nullable=False),
            Column("issue_key", String(40), nullable=True),
            Column("last_error", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("next_attempt_at", DateTime(timezone=True), nullable=False, index=True),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _ticket_json(t: TicketFields) -> dict[str, Any]:
    return {
        "summary": t.summary,
        "description": t.description,
        "severity": t.severity,
        "site": t.site,
        "category": t.category,
    }


def _ticket(d: dict[str, Any]) -> TicketFields:
    return TicketFields(d["summary"], d["description"], d.get("severity"), d.get("site"), d.get("category"))


class SqlProposalRepository:
    def __init__(self, session: AsyncSession, tables: JiraTables) -> None:
        self._s, self._t = session, tables.proposals

    async def get(self, approval_id: str) -> Proposal | None:
        r = (await self._s.execute(select(self._t).where(self._t.c.approval_id == approval_id))).first()
        if r is None:
            return None
        return Proposal(
            r.approval_id,
            r.run_id,
            r.thread_id,
            Action(r.action),
            _ticket(r.ticket),
            r.link_to,
            r.reporter_token,
        )

    async def add(self, p: Proposal) -> None:
        await self._s.execute(
            insert(self._t).values(
                approval_id=p.approval_id,
                run_id=p.run_id,
                thread_id=p.thread_id,
                action=p.action.value,
                ticket=_ticket_json(p.ticket),
                link_to=p.link_to,
                reporter_token=p.reporter_token,
            )
        )


class SqlJobRepository:
    def __init__(self, session: AsyncSession, tables: JiraTables) -> None:
        self._s, self._t = session, tables.jobs

    def _job(self, r: Any) -> Job:
        return Job(
            id=r.id,
            run_id=r.run_id,
            thread_id=r.thread_id,
            action=Action(r.action),
            ticket=_ticket(r.ticket),
            created_at=_aware(r.created_at),
            next_attempt_at=_aware(r.next_attempt_at),
            link_to=r.link_to,
            reporter_token=r.reporter_token,
            status=JobStatus(r.status),
            attempts=r.attempts,
            issue_key=r.issue_key,
            last_error=r.last_error,
        )

    def _row(self, j: Job) -> dict[str, Any]:
        return {
            "run_id": j.run_id,
            "thread_id": j.thread_id,
            "action": j.action.value,
            "ticket": _ticket_json(j.ticket),
            "link_to": j.link_to,
            "reporter_token": j.reporter_token,
            "status": j.status.value,
            "attempts": j.attempts,
            "issue_key": j.issue_key,
            "last_error": j.last_error,
            "created_at": j.created_at,
            "next_attempt_at": j.next_attempt_at,
        }

    async def get(self, job_id: str) -> Job | None:
        r = (await self._s.execute(select(self._t).where(self._t.c.id == job_id))).first()
        return self._job(r) if r else None

    async def add(self, job: Job) -> None:
        await self._s.execute(insert(self._t).values(id=job.id, **self._row(job)))

    async def save(self, job: Job) -> None:
        await self._s.execute(update(self._t).where(self._t.c.id == job.id).values(**self._row(job)))

    async def due(self, now: datetime, limit: int) -> list[Job]:
        rows = await self._s.execute(
            select(self._t)
            .where(self._t.c.status == JobStatus.PENDING.value, self._t.c.next_attempt_at <= now)
            .order_by(self._t.c.created_at)
            .limit(limit)
        )
        return [self._job(r) for r in rows]

    async def pending_count(self) -> int:
        q = select(func.count()).select_from(self._t).where(self._t.c.status == JobStatus.PENDING.value)
        return int((await self._s.execute(q)).scalar() or 0)


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    proposals: ProposalRepository
    jobs: JobRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: JiraTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.proposals = SqlProposalRepository(self._session, self._tables)
        self.jobs = SqlJobRepository(self._session, self._tables)
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
