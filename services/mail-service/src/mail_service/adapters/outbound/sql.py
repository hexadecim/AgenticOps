"""SQLAlchemy adapters for schema `mail`. `dedupe_key` is unique, so a message is stored at most once even if
two pollers race."""

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
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.mail import Address, Attachment, IncomingMail, StoredEmail
from ...domain.status import PollState
from ...ports import EmailRepository, EventRecorder, PollStateRepository

SERVICE = "mail-service"


class MailTables:
    def __init__(self, schema: str | None = "mail") -> None:
        self.metadata = MetaData(schema=schema)
        self.emails = Table(
            "emails",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("dedupe_key", String(600), nullable=False, unique=True),
            Column("thread_id", String(36), nullable=False, index=True),
            Column("provider", String(20), nullable=False),
            Column("provider_id", String(255), nullable=False),
            Column("provider_thread_id", String(255), nullable=True, index=True),
            Column("message_id", String(512), nullable=True, index=True),
            Column("in_reply_to", String(512), nullable=True),
            Column("references", JSON, nullable=False),
            Column("subject", Text, nullable=False),
            Column("sender", JSON, nullable=False),
            Column("recipients", JSON, nullable=False),
            Column("body_text", Text, nullable=False),
            Column("body_html", Text, nullable=True),
            Column("attachments", JSON, nullable=False),
            Column("received_at", DateTime(timezone=True), nullable=False),
            Column("fetched_at", DateTime(timezone=True), nullable=False, index=True),
        )
        self.poll_state = Table(
            "poll_state",
            self.metadata,
            Column("id", Integer, primary_key=True),
            Column("fingerprint", String(64), nullable=True),
            Column("cursor", JSON, nullable=True),
            Column("last_poll_at", DateTime(timezone=True), nullable=True),
            Column("last_success_at", DateTime(timezone=True), nullable=True),
            Column("last_error", Text, nullable=True),
            Column("consecutive_failures", Integer, nullable=False),
            Column("last_new_count", Integer, nullable=False),
            Column("total_received", Integer, nullable=False),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _addr(a: Address) -> dict[str, str]:
    return {"email": a.email, "name": a.name}


def _addrs(items: list[dict[str, str]]) -> tuple[Address, ...]:
    return tuple(Address(i["email"], i.get("name", "")) for i in items)


class SqlEmailRepository:
    def __init__(self, session: AsyncSession, tables: MailTables) -> None:
        self._s, self._t = session, tables.emails

    async def exists(self, dedupe_key: str) -> bool:
        found = await self._s.execute(select(self._t.c.id).where(self._t.c.dedupe_key == dedupe_key))
        return found.first() is not None

    async def add(self, email: StoredEmail) -> None:
        m = email.mail
        await self._s.execute(
            insert(self._t).values(
                id=email.id,
                dedupe_key=m.dedupe_key(email.provider),
                thread_id=email.thread_id,
                provider=email.provider,
                provider_id=m.provider_id,
                provider_thread_id=m.provider_thread_id,
                message_id=m.message_id,
                in_reply_to=m.in_reply_to,
                references=list(m.references),
                subject=m.subject,
                sender=_addr(m.sender),
                recipients={"to": [_addr(a) for a in m.to], "cc": [_addr(a) for a in m.cc]},
                body_text=m.body_text,
                body_html=m.body_html,
                attachments=[
                    {"filename": a.filename, "content_type": a.content_type, "size": a.size}
                    for a in m.attachments
                ],
                received_at=m.received_at,
                fetched_at=email.fetched_at,
            )
        )

    async def get(self, email_id: str) -> StoredEmail | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.id == email_id))).first()
        if row is None:
            return None
        mail = IncomingMail(
            provider_id=row.provider_id,
            message_id=row.message_id,
            subject=row.subject,
            sender=Address(row.sender["email"], row.sender.get("name", "")),
            received_at=_aware(row.received_at),  # type: ignore[arg-type]
            body_text=row.body_text,
            to=_addrs(row.recipients.get("to", [])),
            cc=_addrs(row.recipients.get("cc", [])),
            in_reply_to=row.in_reply_to,
            references=tuple(row.references),
            provider_thread_id=row.provider_thread_id,
            body_html=row.body_html,
            attachments=tuple(
                Attachment(a["filename"], a["content_type"], a["size"]) for a in row.attachments
            ),
        )
        return StoredEmail(row.id, row.thread_id, row.provider, _aware(row.fetched_at), mail)  # type: ignore[arg-type]

    async def thread_of_message(self, message_ids: list[str]) -> str | None:
        rows = await self._s.execute(
            select(self._t.c.thread_id, self._t.c.message_id).where(self._t.c.message_id.in_(message_ids))
        )
        found = {r.message_id: r.thread_id for r in rows}
        return next((found[i] for i in message_ids if i in found), None)  # closest parent wins

    async def thread_of_provider_thread(self, provider: str, provider_thread_id: str) -> str | None:
        row = (
            await self._s.execute(
                select(self._t.c.thread_id)
                .where(self._t.c.provider == provider, self._t.c.provider_thread_id == provider_thread_id)
                .limit(1)
            )
        ).first()
        return row.thread_id if row else None

    async def delete_fetched_before(self, cutoff: datetime) -> int:
        result = await self._s.execute(delete(self._t).where(self._t.c.fetched_at < cutoff))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class SqlPollStateRepository:
    _ID = 1

    def __init__(self, session: AsyncSession, tables: MailTables) -> None:
        self._s, self._t = session, tables.poll_state

    async def get(self) -> PollState:
        row = (await self._s.execute(select(self._t).where(self._t.c.id == self._ID))).first()
        if row is None:
            return PollState()
        return PollState(
            fingerprint=row.fingerprint,
            cursor=row.cursor,
            last_poll_at=_aware(row.last_poll_at),
            last_success_at=_aware(row.last_success_at),
            last_error=row.last_error,
            consecutive_failures=row.consecutive_failures,
            last_new_count=row.last_new_count,
            total_received=row.total_received,
        )

    async def save(self, state: PollState) -> None:
        values: dict[str, Any] = {
            "fingerprint": state.fingerprint,
            "cursor": state.cursor,
            "last_poll_at": state.last_poll_at,
            "last_success_at": state.last_success_at,
            "last_error": state.last_error,
            "consecutive_failures": state.consecutive_failures,
            "last_new_count": state.last_new_count,
            "total_received": state.total_received,
        }
        result = await self._s.execute(update(self._t).where(self._t.c.id == self._ID).values(**values))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            await self._s.execute(insert(self._t).values(id=self._ID, **values))


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    emails: EmailRepository
    poll_state: PollStateRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: MailTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.emails = SqlEmailRepository(self._session, self._tables)
        self.poll_state = SqlPollStateRepository(self._session, self._tables)
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
