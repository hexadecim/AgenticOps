"""SQLAlchemy adapters for schema `approval`."""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.catalogue import Gate
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.approval import Approval, Outcome, Status
from ...ports import ApprovalRepository, EventRecorder

SERVICE = "approval-service"


class ApprovalTables:
    def __init__(self, schema: str | None = "approval") -> None:
        self.metadata = MetaData(schema=schema)
        self.approvals = Table(
            "approvals",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("run_id", String(36), nullable=False, index=True),
            Column("thread_id", String(36), nullable=False, index=True),
            Column("gate", String(10), nullable=False),
            Column("status", String(12), nullable=False, index=True),
            Column("title", Text, nullable=False),
            Column("detail", Text, nullable=False),
            Column("site", String(80), nullable=True),
            Column("severity", String(8), nullable=True),
            Column("jira_key", String(40), nullable=True),
            Column("proposal", JSON, nullable=False),
            Column("requested_at", DateTime(timezone=True), nullable=False, index=True),
            Column("decided_at", DateTime(timezone=True), nullable=True),
            Column("decided_by", String(36), nullable=True),
            Column("decided_by_role", String(40), nullable=True),
            Column("reason", Text, nullable=True),
            Column("edits", JSON, nullable=False),
            Column("outcome", String(20), nullable=True),
            Column("outcome_detail", Text, nullable=True),
            Column("reminders", Integer, nullable=False, default=0),
            Column("reminded_at", DateTime(timezone=True), nullable=True),
            Column("health", String(12), nullable=True),
            Column("health_detail", Text, nullable=True),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _row(a: Approval) -> dict[str, Any]:
    return {
        "run_id": a.run_id,
        "thread_id": a.thread_id,
        "gate": a.gate.value,
        "status": a.status.value,
        "title": a.title,
        "detail": a.detail,
        "site": a.site,
        "severity": a.severity,
        "jira_key": a.jira_key,
        "proposal": a.proposal,
        "requested_at": a.requested_at,
        "decided_at": a.decided_at,
        "decided_by": a.decided_by,
        "decided_by_role": a.decided_by_role,
        "reason": a.reason,
        "edits": a.edits,
        "outcome": a.outcome.value if a.outcome else None,
        "outcome_detail": a.outcome_detail,
        "reminders": a.reminders,
        "reminded_at": a.reminded_at,
        "health": a.health,
        "health_detail": a.health_detail,
    }


def _approval(r: Any) -> Approval:
    return Approval(
        id=r.id,
        run_id=r.run_id,
        thread_id=r.thread_id,
        gate=Gate(r.gate),
        title=r.title,
        detail=r.detail,
        requested_at=_aware(r.requested_at),  # type: ignore[arg-type]
        site=r.site,
        severity=r.severity,
        jira_key=r.jira_key,
        proposal=dict(r.proposal),
        status=Status(r.status),
        decided_at=_aware(r.decided_at),
        decided_by=r.decided_by,
        decided_by_role=r.decided_by_role,
        reason=r.reason,
        edits=dict(r.edits),
        outcome=Outcome(r.outcome) if r.outcome else None,
        outcome_detail=r.outcome_detail,
        reminders=r.reminders or 0,
        reminded_at=_aware(r.reminded_at),
        health=r.health,
        health_detail=r.health_detail,
    )


class SqlApprovalRepository:
    def __init__(self, session: AsyncSession, tables: ApprovalTables) -> None:
        self._s, self._t = session, tables.approvals

    async def get(self, approval_id: str) -> Approval | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.id == approval_id))).first()
        return _approval(row) if row else None

    async def add(self, approval: Approval) -> None:
        await self._s.execute(insert(self._t).values(id=approval.id, **_row(approval)))

    async def save(self, approval: Approval) -> None:
        await self._s.execute(update(self._t).where(self._t.c.id == approval.id).values(**_row(approval)))

    async def list(
        self, *, statuses: set[Status], gate: Gate | None = None, limit: int = 200
    ) -> list[Approval]:
        query = select(self._t).where(self._t.c.status.in_([s.value for s in statuses]))
        if gate:
            query = query.where(self._t.c.gate == gate.value)
        rows = await self._s.execute(query.order_by(self._t.c.requested_at).limit(limit))
        return [_approval(r) for r in rows]

    async def latest_for_run(self, run_id: str, gate: Gate) -> Approval | None:
        t = self._t
        row = (
            await self._s.execute(
                select(t)
                .where(t.c.run_id == run_id, t.c.gate == gate.value)
                .order_by(t.c.requested_at.desc())
            )
        ).first()
        return _approval(row) if row else None


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    approvals: ApprovalRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: ApprovalTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.approvals = SqlApprovalRepository(self._session, self._tables)
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
