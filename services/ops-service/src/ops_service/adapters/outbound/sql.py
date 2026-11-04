"""SQLAlchemy adapters for schema `ops`: fix proposals and jobs, probe states, the runbook index (pgvector on
Postgres; JSON + Python cosine elsewhere, for tests), outbox and inbox."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import TypeDecorator, TypeEngine

from ...domain.diagnostics import cosine
from ...domain.model import FixJob, FixProposal, JobStatus, RunbookChunk
from ...ports import EventRecorder, JobRepository, ProbeStateRepository, ProposalRepository

SERVICE = "ops-service"


class Embedding(TypeDecorator[list[float]]):
    """pgvector's `vector` on Postgres; a JSON list anywhere else."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else [float(v) for v in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [float(v) for v in json.loads(value)]
        return [float(v) for v in value]


class OpsTables:
    def __init__(self, schema: str | None = "ops") -> None:
        self.metadata = MetaData(schema=schema)
        self.proposals = Table(
            "fix_proposals",
            self.metadata,
            Column("approval_id", String(36), primary_key=True),
            Column("run_id", String(36), nullable=False),
            Column("thread_id", String(36), nullable=False),
            Column("action", String(60), nullable=False),
            Column("params", JSON, nullable=False),
            Column("choices", JSON, nullable=False),
            Column("services", JSON, nullable=False),
            Column("region", String(40), nullable=True),
            Column("site", String(40), nullable=True),
            Column("jira_key", String(40), nullable=True),
        )
        self.jobs = Table(
            "fix_jobs",
            self.metadata,
            Column("trace", String(64), nullable=True),
            Column("id", String(36), primary_key=True),
            Column("run_id", String(36), nullable=False),
            Column("thread_id", String(36), nullable=False),
            Column("action", String(60), nullable=False),
            Column("params", JSON, nullable=False),
            Column("status", String(12), nullable=False, index=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("next_attempt_at", DateTime(timezone=True), nullable=False, index=True),
            Column("attempts", Integer, nullable=False),
            Column("jira_key", String(40), nullable=True),
            Column("region", String(40), nullable=True),
            Column("ok", Boolean, nullable=True),
            Column("detail", Text, nullable=True),
        )
        self.probe_states = Table(
            "probe_states",
            self.metadata,
            Column("probe", String(60), primary_key=True),
            Column("region", String(40), primary_key=True),  # "" when the probe has no region
            Column("ok", Boolean, nullable=False),
            Column("changed_at", DateTime(timezone=True), nullable=False),
        )
        self.index_meta = Table(
            "runbook_index",
            self.metadata,
            Column("id", Integer, primary_key=True),
            Column("fingerprint", String(64), nullable=False),
            Column("indexed_at", DateTime(timezone=True), nullable=False),
        )
        self.chunks = Table(
            "runbook_chunks",
            self.metadata,
            Column("runbook_id", String(40), primary_key=True),
            Column("idx", Integer, primary_key=True),
            Column("heading", Text, nullable=False),
            Column("text", Text, nullable=False),
            Column("embedding", Embedding, nullable=False),
        )
        # State of the mock environment (adapters/outbound/mock_world.py): open faults and executed actions.
        self.mock_faults = Table(
            "mock_faults",
            self.metadata,
            Column("service", String(60), primary_key=True),
            Column("region", String(40), primary_key=True),
            Column("detail", Text, nullable=False),
            Column("since", DateTime(timezone=True), nullable=False),
            Column("cleared_at", DateTime(timezone=True), nullable=True),
        )
        self.mock_runs = Table(
            "mock_runs",
            self.metadata,
            Column("key", String(64), primary_key=True),
            Column("action", String(60), nullable=False),
            Column("params", JSON, nullable=False),
            Column("ok", Boolean, nullable=False),
            Column("detail", Text, nullable=False),
            Column("ran_at", DateTime(timezone=True), nullable=False),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class SqlProposals:
    def __init__(self, session: AsyncSession, tables: OpsTables) -> None:
        self._s, self._t = session, tables.proposals

    async def get(self, approval_id: str) -> FixProposal | None:
        r = (await self._s.execute(select(self._t).where(self._t.c.approval_id == approval_id))).first()
        if r is None:
            return None
        return FixProposal(
            r.approval_id, r.run_id, r.thread_id, r.action, dict(r.params), dict(r.choices), r.region,
            r.site, r.jira_key, list(r.services),
        )  # fmt: skip

    async def add(self, p: FixProposal) -> None:
        await self._s.execute(
            insert(self._t).values(
                approval_id=p.approval_id, run_id=p.run_id, thread_id=p.thread_id, action=p.action,
                params=p.params, choices=p.choices, services=p.services, region=p.region, site=p.site,
                jira_key=p.jira_key,
            )
        )  # fmt: skip


def _job_row(j: FixJob) -> dict[str, Any]:
    return {
        "run_id": j.run_id,
        "thread_id": j.thread_id,
        "action": j.action,
        "params": j.params,
        "status": j.status.value,
        "created_at": j.created_at,
        "next_attempt_at": j.next_attempt_at,
        "attempts": j.attempts,
        "jira_key": j.jira_key,
        "region": j.region,
        "ok": j.ok,
        "detail": j.detail,
        "trace": j.trace,
    }


def _job(r: Any) -> FixJob:
    return FixJob(
        id=r.id,
        run_id=r.run_id,
        thread_id=r.thread_id,
        action=r.action,
        params=dict(r.params),
        created_at=aware(r.created_at),  # type: ignore[arg-type]
        next_attempt_at=aware(r.next_attempt_at),  # type: ignore[arg-type]
        jira_key=r.jira_key,
        region=r.region,
        status=JobStatus(r.status),
        attempts=r.attempts,
        detail=r.detail,
        ok=r.ok,
        trace=r.trace,
    )


class SqlJobs:
    def __init__(self, session: AsyncSession, tables: OpsTables) -> None:
        self._s, self._t = session, tables.jobs

    async def get(self, job_id: str) -> FixJob | None:
        r = (await self._s.execute(select(self._t).where(self._t.c.id == job_id))).first()
        return _job(r) if r else None

    async def add(self, job: FixJob) -> None:
        await self._s.execute(insert(self._t).values(id=job.id, **_job_row(job)))

    async def save(self, job: FixJob) -> None:
        await self._s.execute(update(self._t).where(self._t.c.id == job.id).values(**_job_row(job)))

    async def due(self, now: datetime, limit: int) -> list[FixJob]:
        rows = await self._s.execute(
            select(self._t)
            .where(self._t.c.status == JobStatus.PENDING.value, self._t.c.next_attempt_at <= now)
            .order_by(self._t.c.next_attempt_at)
            .limit(limit)
        )
        return [_job(r) for r in rows]


class SqlProbeStates:
    def __init__(self, session: AsyncSession, tables: OpsTables) -> None:
        self._s, self._t = session, tables.probe_states

    async def last(self, probe: str, region: str | None) -> bool | None:
        r = (
            await self._s.execute(
                select(self._t.c.ok).where(self._t.c.probe == probe, self._t.c.region == (region or ""))
            )
        ).first()
        return bool(r.ok) if r else None

    async def set(self, probe: str, region: str | None, ok: bool, now: datetime) -> None:
        key = (self._t.c.probe == probe, self._t.c.region == (region or ""))
        result = await self._s.execute(update(self._t).where(*key).values(ok=ok, changed_at=now))
        if not result.rowcount:  # type: ignore[attr-defined]
            await self._s.execute(
                insert(self._t).values(probe=probe, region=region or "", ok=ok, changed_at=now)
            )


class SqlRunbookIndex:
    """Nearest-neighbour search over runbook chunks: pgvector's cosine distance on Postgres."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OpsTables) -> None:
        self._sessions, self._t = sessions, tables

    async def fingerprint(self) -> str | None:
        async with self._sessions() as s:
            r = (await s.execute(select(self._t.index_meta.c.fingerprint))).first()
        return str(r.fingerprint) if r else None

    async def replace(self, fingerprint: str, chunks: list[tuple[RunbookChunk, list[float]]]) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(delete(self._t.chunks))
            await s.execute(delete(self._t.index_meta))
            for chunk, vector in chunks:
                await s.execute(
                    insert(self._t.chunks).values(
                        runbook_id=chunk.runbook_id, idx=chunk.index, heading=chunk.heading, text=chunk.text,
                        embedding=vector,
                    )
                )  # fmt: skip
            await s.execute(
                insert(self._t.index_meta).values(id=1, fingerprint=fingerprint, indexed_at=datetime.now(UTC))
            )

    async def nearest(self, vector: list[float], limit: int) -> list[tuple[str, int, float]]:
        c = self._t.chunks.c
        async with self._sessions() as s:
            if s.get_bind().dialect.name == "postgresql":
                from pgvector.sqlalchemy import Vector

                # pgvector's cosine distance operator; the TypeDecorator hides Vector's comparator methods.
                distance = c.embedding.op("<=>", return_type=Float())(literal(vector, Vector()))
                rows = await s.execute(
                    select(c.runbook_id, c.idx, distance.label("d")).order_by("d").limit(limit)
                )
                return [(r.runbook_id, r.idx, 1.0 - float(r.d)) for r in rows]
            rows = await s.execute(select(c.runbook_id, c.idx, c.embedding))
            scored = [(r.runbook_id, r.idx, cosine(vector, r.embedding)) for r in rows]
        return sorted(scored, key=lambda x: x[2], reverse=True)[:limit]


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
    probe_states: ProbeStateRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: OpsTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.proposals = SqlProposals(self._session, self._tables)
        self.jobs = SqlJobs(self._session, self._tables)
        self.probe_states = SqlProbeStates(self._session, self._tables)
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
