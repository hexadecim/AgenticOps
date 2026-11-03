"""SQLAlchemy adapters for schema `reporting` (read models only; rebuilt from events if ever lost)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.events import inbox_table
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.model import Case, Dependency, Probe, TimelineEntry
from ...ports import CaseStore, DependencyStore, EmailStore, ProbeStore, TimelineStore

_OPEN = (
    "queued",
    "running",
    "resuming",
    "waiting_llm",
    "waiting_privacy",
    "waiting_approval",
    "approved",
    "ticket_created",
    "waiting_ops",
    "fix_approved",
    "fix_rejected",
    "fix_failed",
)
_CASE_TIMES = (
    "received_at",
    "queued_at",
    "triaged_at",
    "requested_at",
    "decided_at",
    "ticket_at",
    "updated_at",
)


class ReportingTables:
    def __init__(self, schema: str | None = "reporting") -> None:
        self.metadata = MetaData(schema=schema)
        self.cases = Table(
            "cases",
            self.metadata,
            Column("run_id", String(36), primary_key=True),
            Column("thread_id", String(36), nullable=False, index=True),
            Column("email_id", String(36), nullable=True, index=True),
            Column("status", String(20), nullable=False, index=True),
            *(Column(name, DateTime(timezone=True), nullable=True) for name in _CASE_TIMES),
            Column("decision", String(12), nullable=True),
            Column("outcome", String(12), nullable=True),
            Column("jira_key", String(40), nullable=True, index=True),
            Column("title", Text, nullable=True),
            Column("severity", String(8), nullable=True),
            Column("site", String(80), nullable=True),
            Column("paused", Boolean, nullable=False),
        )
        self.emails = Table(
            "emails",
            self.metadata,
            Column("email_id", String(36), primary_key=True),
            Column("received_at", DateTime(timezone=True), nullable=False),
        )
        self.timeline = Table(
            "timeline",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("at", DateTime(timezone=True), nullable=False, index=True),
            Column("actor", String(80), nullable=False),
            Column("text", Text, nullable=False),
            Column("run_id", String(36), nullable=True, index=True),
            Column("thread_id", String(36), nullable=True, index=True),
            Column("jira_key", String(40), nullable=True),
            Column("system", Boolean, nullable=False),
        )
        self.dependencies = Table(
            "dependencies",
            self.metadata,
            Column("name", String(60), primary_key=True),
            Column("states", JSON, nullable=False),
            Column("since", DateTime(timezone=True), nullable=True),
        )
        self.probes = Table(
            "probes",
            self.metadata,
            Column("name", String(80), primary_key=True),
            Column("region", String(40), primary_key=True),
            Column("ok", Boolean, nullable=False),
            Column("detail", Text, nullable=False),
            Column("latency_ms", Integer, nullable=True),
            Column("since", DateTime(timezone=True), nullable=True),
        )
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _case(r: Any) -> Case:
    return Case(
        run_id=r.run_id,
        thread_id=r.thread_id,
        status=r.status,
        email_id=r.email_id,
        decision=r.decision,
        outcome=r.outcome,
        jira_key=r.jira_key,
        title=r.title,
        severity=r.severity,
        site=r.site,
        paused=r.paused,
        **{name: _aware(getattr(r, name)) for name in _CASE_TIMES},
    )


def _case_row(c: Case) -> dict[str, Any]:
    return {
        "thread_id": c.thread_id,
        "email_id": c.email_id,
        "status": c.status,
        "decision": c.decision,
        "outcome": c.outcome,
        "jira_key": c.jira_key,
        "title": c.title,
        "severity": c.severity,
        "site": c.site,
        "paused": c.paused,
        **{name: getattr(c, name) for name in _CASE_TIMES},
    }


class SqlCaseStore:
    def __init__(self, session: AsyncSession, tables: ReportingTables) -> None:
        self._s, self._t = session, tables.cases

    async def get(self, run_id: str) -> Case | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.run_id == run_id))).first()
        return _case(row) if row else None

    async def save(self, case: Case) -> None:
        result = await self._s.execute(
            update(self._t).where(self._t.c.run_id == case.run_id).values(**_case_row(case))
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            await self._s.execute(insert(self._t).values(run_id=case.run_id, **_case_row(case)))

    async def by_email(self, email_id: str) -> list[Case]:
        return [
            _case(r) for r in await self._s.execute(select(self._t).where(self._t.c.email_id == email_id))
        ]

    async def by_ticket(self, jira_key: str) -> list[Case]:
        return [
            _case(r) for r in await self._s.execute(select(self._t).where(self._t.c.jira_key == jira_key))
        ]

    async def active_or_since(self, since: datetime) -> list[Case]:
        t = self._t
        rows = await self._s.execute(
            select(t).where(or_(t.c.status.in_(_OPEN), t.c.updated_at >= since, t.c.queued_at >= since))
        )
        return [_case(r) for r in rows]

    async def incidents(self, limit: int) -> list[Case]:
        t = self._t
        rows = await self._s.execute(
            select(t)
            .where(t.c.jira_key.is_not(None), t.c.outcome.in_(["created", "linked"]))
            .order_by(t.c.ticket_at.desc())
            .limit(limit)
        )
        return [_case(r) for r in rows]


class SqlEmailStore:
    def __init__(self, session: AsyncSession, tables: ReportingTables) -> None:
        self._s, self._t = session, tables.emails

    async def received(self, email_id: str) -> datetime | None:
        row = (
            await self._s.execute(select(self._t.c.received_at).where(self._t.c.email_id == email_id))
        ).first()
        return _aware(row.received_at) if row else None

    async def record(self, email_id: str, at: datetime) -> None:
        if await self.received(email_id) is None:
            await self._s.execute(insert(self._t).values(email_id=email_id, received_at=at))


class SqlTimelineStore:
    def __init__(self, session: AsyncSession, tables: ReportingTables) -> None:
        self._s, self._t = session, tables.timeline

    async def add(self, entry: TimelineEntry) -> None:
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(self._t).values(
                        id=entry.id,
                        at=entry.at,
                        actor=entry.actor[:80],
                        text=entry.text,
                        run_id=entry.run_id,
                        thread_id=entry.thread_id,
                        jira_key=entry.jira_key,
                        system=entry.system,
                    )
                )
        except IntegrityError:
            pass  # the same event projected twice

    async def for_cases(
        self, run_ids: list[str], thread_ids: list[str], *, start: datetime, end: datetime
    ) -> list[TimelineEntry]:
        t = self._t
        rows = await self._s.execute(
            select(t)
            .where(
                or_(
                    t.c.run_id.in_(run_ids),
                    t.c.thread_id.in_(thread_ids),
                    and_(t.c.system, t.c.at >= start, t.c.at <= end),
                )
            )
            .order_by(t.c.at, t.c.id)
            .limit(200)
        )
        return [
            TimelineEntry(r.id, _aware(r.at), r.actor, r.text, r.run_id, r.thread_id, r.jira_key, r.system)  # type: ignore[arg-type]
            for r in rows
        ]


class SqlDependencyStore:
    def __init__(self, session: AsyncSession, tables: ReportingTables) -> None:
        self._s, self._t = session, tables.dependencies

    async def get(self, name: str) -> Dependency | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.name == name))).first()
        return Dependency(row.name, dict(row.states), _aware(row.since)) if row else None

    async def save(self, dependency: Dependency) -> None:
        values = {"states": dependency.states, "since": dependency.since}
        result = await self._s.execute(
            update(self._t).where(self._t.c.name == dependency.name).values(**values)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            await self._s.execute(insert(self._t).values(name=dependency.name, **values))

    async def all(self) -> list[Dependency]:
        return [
            Dependency(r.name, dict(r.states), _aware(r.since))
            for r in await self._s.execute(select(self._t))
        ]


class SqlProbeStore:
    def __init__(self, session: AsyncSession, tables: ReportingTables) -> None:
        self._s, self._t = session, tables.probes

    @staticmethod
    def _probe(r: Any) -> Probe:
        return Probe(r.name, r.region, r.ok, r.detail, r.latency_ms, _aware(r.since))

    async def get(self, name: str, region: str) -> Probe | None:
        t = self._t
        row = (await self._s.execute(select(t).where(t.c.name == name, t.c.region == region))).first()
        return self._probe(row) if row else None

    async def save(self, probe: Probe) -> None:
        t = self._t
        values = {
            "ok": probe.ok,
            "detail": probe.detail,
            "latency_ms": probe.latency_ms,
            "since": probe.since,
        }
        result = await self._s.execute(
            update(t).where(t.c.name == probe.name, t.c.region == probe.region).values(**values)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            await self._s.execute(insert(t).values(name=probe.name, region=probe.region, **values))

    async def all(self) -> list[Probe]:
        t = self._t
        return [self._probe(r) for r in await self._s.execute(select(t).order_by(t.c.region, t.c.name))]


class SqlUnitOfWork:
    cases: CaseStore
    emails: EmailStore
    timeline: TimelineStore
    dependencies: DependencyStore
    probes: ProbeStore

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tables: ReportingTables,
        session: AsyncSession | None = None,
    ) -> None:
        self._sessions, self._tables, self._given = sessions, tables, session

    async def __aenter__(self) -> Self:
        """With `session` given (inside the inbox transaction) the caller owns commit and rollback."""
        self._session = self._given or self._sessions()
        if self._given is None:
            await self._session.begin()
        t = self._tables
        self.cases, self.emails = SqlCaseStore(self._session, t), SqlEmailStore(self._session, t)
        self.timeline, self.dependencies = (
            SqlTimelineStore(self._session, t),
            SqlDependencyStore(self._session, t),
        )
        self.probes = SqlProbeStore(self._session, t)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._given is None:
            if self._session.in_transaction():
                await self._session.rollback()
            await self._session.close()

    async def commit(self) -> None:
        if self._given is None:
            await self._session.commit()
            await self._session.begin()
