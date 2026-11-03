"""SQLAlchemy adapter for schema `audit`. Only INSERT and SELECT are ever issued; in Postgres a trigger
(migration 0001) rejects UPDATE, DELETE and TRUNCATE on `entries` whoever tries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opspilot_common.events import inbox_table
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, Text, and_, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.entry import AuditEntry, AuditPage, AuditQuery, Kind


class AuditTables:
    def __init__(self, schema: str | None = "audit") -> None:
        self.metadata = MetaData(schema=schema)
        self.entries = Table(
            "entries",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("occurred_at", DateTime(timezone=True), nullable=False, index=True),
            Column("recorded_at", DateTime(timezone=True), nullable=False),
            Column("kind", String(10), nullable=False, index=True),
            Column("actor", String(80), nullable=False),
            Column("actor_id", String(80), nullable=True),
            Column("event", String(80), nullable=False),
            Column("detail", Text, nullable=False),
            Column("privacy", String(200), nullable=False),
            Column("ticket", String(40), nullable=True, index=True),
            Column("thread_id", String(36), nullable=True, index=True),
            Column("event_type", String(80), nullable=False),
            Column("source", String(80), nullable=False),
            Column("correlation_id", String(64), nullable=True),
            Column("payload", JSON, nullable=False),
        )
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlAuditLog:
    def __init__(self, session: AsyncSession, tables: AuditTables) -> None:
        self._s, self._t = session, tables.entries

    async def append(self, entry: AuditEntry) -> bool:
        try:
            async with self._s.begin_nested():
                await self._s.execute(
                    insert(self._t).values(
                        id=entry.id,
                        occurred_at=entry.occurred_at,
                        recorded_at=datetime.now(UTC),
                        kind=entry.kind.value,
                        actor=entry.actor[:80],
                        actor_id=entry.actor_id,
                        event=entry.event[:80],
                        detail=entry.detail,
                        privacy=entry.privacy[:200],
                        ticket=entry.ticket,
                        thread_id=entry.thread_id,
                        event_type=entry.event_type,
                        source=entry.source,
                        correlation_id=entry.correlation_id,
                        payload=entry.payload,
                    )
                )
        except IntegrityError:
            return False
        return True

    async def search(self, query: AuditQuery) -> AuditPage:
        t = self._t
        conditions: list[Any] = []
        if query.kind:
            conditions.append(t.c.kind == query.kind.value)
        if query.text:
            like = f"%{_escape(query.text)}%"
            conditions.append(
                or_(
                    *(
                        col.ilike(like, escape="\\")
                        for col in (t.c.actor, t.c.event, t.c.detail, t.c.ticket, t.c.thread_id)
                    )
                )
            )
        if query.before:
            at, ident = query.before
            conditions.append(or_(t.c.occurred_at < at, and_(t.c.occurred_at == at, t.c.id < ident)))
        rows = (
            await self._s.execute(
                select(t)
                .where(*conditions)
                .order_by(t.c.occurred_at.desc(), t.c.id.desc())
                .limit(query.limit + 1)
            )
        ).all()
        entries = [
            AuditEntry(
                id=r.id,
                occurred_at=_aware(r.occurred_at),
                kind=Kind(r.kind),
                actor=r.actor,
                event=r.event,
                detail=r.detail,
                privacy=r.privacy,
                event_type=r.event_type,
                source=r.source,
                actor_id=r.actor_id,
                ticket=r.ticket,
                thread_id=r.thread_id,
                correlation_id=r.correlation_id,
                payload=dict(r.payload),
            )
            for r in rows[: query.limit]
        ]
        more = len(rows) > query.limit
        return AuditPage(entries, (entries[-1].occurred_at, entries[-1].id) if more and entries else None)


class SqlAuditReader:
    """Searches in a short read-only session of its own."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: AuditTables) -> None:
        self._sessions, self._tables = sessions, tables

    async def search(self, query: AuditQuery) -> AuditPage:
        async with self._sessions() as session:
            return await SqlAuditLog(session, self._tables).search(query)
