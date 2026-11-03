"""Transactional outbox (producer side) and inbox (idempotent consumer side).

Write the outbox row in the SAME transaction as the state change; `OutboxRelay` publishes it later.
Consumers wrap handling in `Inbox.first_time(...)` so a redelivered event is processed at most once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..resilience import ResilienceError
from .envelope import EventEnvelope
from .ports import EventHandler, EventPublisher

log = structlog.get_logger(__name__)


def outbox_table(metadata: MetaData, schema: str | None = None) -> Table:
    return Table(
        "outbox",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("type", String(80), nullable=False, index=True),
        Column("body", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, index=True),
        Column("published_at", DateTime(timezone=True), nullable=True, index=True),
        Column("attempts", Integer, nullable=False, default=0),
        Column("last_error", Text, nullable=True),
        schema=schema,
    )


def inbox_table(metadata: MetaData, schema: str | None = None) -> Table:
    return Table(
        "inbox",
        metadata,
        Column("event_id", String(36), primary_key=True),
        Column("consumer", String(80), primary_key=True),
        Column("processed_at", DateTime(timezone=True), nullable=False),
        schema=schema,
    )


class Outbox:
    """Appends events to the outbox table inside the caller's transaction."""

    def __init__(self, table: Table) -> None:
        self._table = table

    async def add(self, session: AsyncSession, envelope: EventEnvelope) -> None:
        await session.execute(
            insert(self._table).values(
                id=envelope.id,
                type=envelope.type,
                body=envelope.model_dump_json(),
                created_at=datetime.now(UTC),
                attempts=0,
            )
        )


class OutboxRelay:
    """Publishes pending outbox rows in creation order; stops a batch at the first failure to keep order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        table: Table,
        publisher: EventPublisher,
        *,
        batch_size: int = 100,
    ) -> None:
        self._sessions = session_factory
        self._table = table
        self._publisher = publisher
        self._batch_size = batch_size

    async def run_once(self) -> int:
        t = self._table
        published = 0
        async with self._sessions() as session, session.begin():
            rows = (
                await session.execute(
                    select(t.c.id, t.c.body)
                    .where(t.c.published_at.is_(None))
                    .order_by(t.c.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                try:
                    await self._publisher.publish(EventEnvelope.model_validate_json(row.body))
                except (ResilienceError, ConnectionError) as exc:
                    await session.execute(
                        update(t)
                        .where(t.c.id == row.id)
                        .values(attempts=t.c.attempts + 1, last_error=str(exc))
                    )
                    log.warning("outbox_publish_deferred", event_id=row.id, error=str(exc))
                    break
                await session.execute(
                    update(t).where(t.c.id == row.id).values(published_at=datetime.now(UTC))
                )
                published += 1
        return published


class Inbox:
    """Records processed event ids per consumer; `first_time` is False for redeliveries."""

    def __init__(self, table: Table, consumer: str) -> None:
        self._table = table
        self._consumer = consumer

    async def first_time(self, session: AsyncSession, event_id: str) -> bool:
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(self._table).values(
                        event_id=event_id, consumer=self._consumer, processed_at=datetime.now(UTC)
                    )
                )
        except IntegrityError:
            return False
        return True

    def guard(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handler: Callable[[AsyncSession, EventEnvelope], Awaitable[None]],
    ) -> EventHandler:
        """Wrap a handler so it runs once per event, in one transaction with the inbox insert."""

        async def guarded(envelope: EventEnvelope) -> None:
            async with session_factory() as session, session.begin():
                if not await self.first_time(session, envelope.id):
                    log.info("event_duplicate_skipped", event_id=envelope.id, consumer=self._consumer)
                    return
                await handler(session, envelope)

        return guarded
