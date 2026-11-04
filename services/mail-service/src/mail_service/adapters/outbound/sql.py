"""SQLAlchemy adapters for schema `mail`. `dedupe_key` is unique, so a message is stored at most once even if
two pollers race."""

from __future__ import annotations

import hashlib
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
    UniqueConstraint,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.dsr_message import DsrMessage
from ...domain.mail import Address, Attachment, IncomingMail, StoredEmail
from ...domain.outgoing import OutgoingMessage, OutgoingStatus
from ...domain.status import PollState
from ...ports import (
    DsrMessageRepository,
    EmailRepository,
    EventRecorder,
    OutgoingRepository,
    PollStateRepository,
)

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
        self.outgoing = Table(
            "outgoing",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("message_id", String(36), nullable=False, index=True),
            Column("approval_id", String(36), nullable=False, index=True),
            Column("run_id", String(36), nullable=False),
            Column("thread_id", String(36), nullable=False),
            Column("email_id", String(36), nullable=False),
            Column("email_token", String(40), nullable=False),
            Column("name_token", String(40), nullable=True),
            Column("subject", Text, nullable=False),
            Column("body", Text, nullable=False),
            Column("jira_key", String(40), nullable=True),
            Column("kind", String(20), nullable=True),
            Column("status", String(10), nullable=False, index=True),
            Column("attempts", Integer, nullable=False),
            Column("detail", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False, index=True),
            Column("next_try_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint("message_id", "email_token", name="uq_outgoing_message_recipient"),
        )
        self.approved_messages = Table(
            "approved_messages",
            self.metadata,
            Column("approval_id", String(36), primary_key=True),
            Column("approved_at", DateTime(timezone=True), nullable=False, index=True),
        )
        self.dsr_messages = Table(
            "dsr_messages",
            self.metadata,
            Column("id", String(60), primary_key=True),
            Column("dsr_id", String(12), nullable=False),
            Column("kind", String(24), nullable=False),
            Column("attempt", Integer, nullable=False),
            Column("status", String(10), nullable=False, index=True),
            Column("tries", Integer, nullable=False),
            Column("detail", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("next_try_at", DateTime(timezone=True), nullable=False),
        )
        self.erased_emails = Table(
            "erased_emails",
            self.metadata,
            Column("key_hash", String(64), primary_key=True),
            Column("erased_at", DateTime(timezone=True), nullable=False),
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
        self._tomb = tables.erased_emails

    async def exists(self, dedupe_key: str) -> bool:
        found = await self._s.execute(select(self._t.c.id).where(self._t.c.dedupe_key == dedupe_key))
        if found.first() is not None:
            return True
        tomb = self._tomb
        hashed = hashlib.sha256(dedupe_key.encode()).hexdigest()
        return (
            await self._s.execute(select(tomb.c.key_hash).where(tomb.c.key_hash == hashed))
        ).first() is not None

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


def _outgoing(r: Any) -> OutgoingMessage:
    return OutgoingMessage(
        id=r.id, message_id=r.message_id, approval_id=r.approval_id, run_id=r.run_id, thread_id=r.thread_id,
        email_id=r.email_id, email_token=r.email_token, name_token=r.name_token, subject=r.subject,
        body=r.body, created_at=r.created_at.replace(tzinfo=r.created_at.tzinfo or UTC),
        next_try_at=r.next_try_at.replace(tzinfo=r.next_try_at.tzinfo or UTC),
        jira_key=r.jira_key, kind=r.kind, status=OutgoingStatus(r.status), attempts=r.attempts,
        detail=r.detail,
    )  # fmt: skip


class SqlOutgoingRepository:
    def __init__(self, session: AsyncSession, tables: MailTables) -> None:
        self._s, self._t, self._approved = session, tables.outgoing, tables.approved_messages

    async def exists(self, message_id: str) -> bool:
        row = await self._s.execute(select(self._t.c.id).where(self._t.c.message_id == message_id).limit(1))
        return row.first() is not None

    async def add(self, message: OutgoingMessage) -> None:
        await self._s.execute(
            insert(self._t).values(
                id=message.id, message_id=message.message_id, approval_id=message.approval_id,
                run_id=message.run_id, thread_id=message.thread_id, email_id=message.email_id,
                email_token=message.email_token, name_token=message.name_token, subject=message.subject,
                body=message.body, jira_key=message.jira_key, kind=message.kind, status=message.status.value,
                attempts=message.attempts, detail=message.detail, created_at=message.created_at,
                next_try_at=message.next_try_at,
            )
        )  # fmt: skip

    async def save(self, message: OutgoingMessage) -> None:
        await self._s.execute(
            update(self._t)
            .where(self._t.c.id == message.id)
            .values(
                status=message.status.value, attempts=message.attempts, detail=message.detail,
                next_try_at=message.next_try_at,
            )
        )  # fmt: skip

    async def approve(self, approval_id: str, at: datetime) -> None:
        seen = await self._s.execute(
            select(self._approved.c.approval_id).where(self._approved.c.approval_id == approval_id)
        )
        if seen.first() is None:
            await self._s.execute(insert(self._approved).values(approval_id=approval_id, approved_at=at))

    async def due(self, now: datetime, limit: int) -> list[OutgoingMessage]:
        rows = await self._s.execute(
            select(self._t)
            .join(self._approved, self._approved.c.approval_id == self._t.c.approval_id)
            .where(self._t.c.status == OutgoingStatus.PENDING.value, self._t.c.next_try_at <= now)
            .order_by(self._t.c.created_at)
            .limit(limit)
        )
        return [_outgoing(r) for r in rows]

    async def of_message(self, message_id: str) -> list[OutgoingMessage]:
        rows = await self._s.execute(select(self._t).where(self._t.c.message_id == message_id))
        return [_outgoing(r) for r in rows]

    async def interrupted(self) -> list[OutgoingMessage]:
        rows = await self._s.execute(select(self._t).where(self._t.c.status == OutgoingStatus.SENDING.value))
        return [_outgoing(r) for r in rows]

    async def delete_created_before(self, cutoff: datetime) -> int:
        await self._s.execute(delete(self._approved).where(self._approved.c.approved_at < cutoff))
        result = await self._s.execute(
            delete(self._t).where(
                self._t.c.created_at < cutoff,
                self._t.c.status.in_([OutgoingStatus.SENT.value, OutgoingStatus.FAILED.value]),
            )
        )
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


class SqlDsrMessageRepository:
    def __init__(self, session: AsyncSession, tables: MailTables) -> None:
        self._s, self._t = session, tables.dsr_messages

    def _row(self, row: Any) -> DsrMessage:
        return DsrMessage(
            row.id, row.dsr_id, row.kind, row.attempt, _aware(row.created_at), _aware(row.next_try_at),  # type: ignore[arg-type]
            OutgoingStatus(row.status), row.tries, row.detail,
        )  # fmt: skip

    async def exists(self, message_id: str) -> bool:
        return (
            await self._s.execute(select(self._t.c.id).where(self._t.c.id == message_id))
        ).first() is not None

    async def add(self, message: DsrMessage) -> None:
        await self._s.execute(
            insert(self._t).values(
                id=message.id, dsr_id=message.dsr_id, kind=message.kind, attempt=message.attempt,
                status=message.status.value, tries=message.tries, detail=message.detail,
                created_at=message.created_at, next_try_at=message.next_try_at,
            )
        )  # fmt: skip

    async def save(self, message: DsrMessage) -> None:
        await self._s.execute(
            update(self._t)
            .where(self._t.c.id == message.id)
            .values(
                status=message.status.value, tries=message.tries, detail=message.detail,
                next_try_at=message.next_try_at,
            )
        )  # fmt: skip

    async def due(self, now: datetime, limit: int) -> list[DsrMessage]:
        rows = await self._s.execute(
            select(self._t)
            .where(self._t.c.status == OutgoingStatus.PENDING.value, self._t.c.next_try_at <= now)
            .order_by(self._t.c.created_at)
            .limit(limit)
        )
        return [self._row(r) for r in rows]

    async def interrupted(self) -> list[DsrMessage]:
        rows = await self._s.execute(select(self._t).where(self._t.c.status == OutgoingStatus.SENDING.value))
        return [self._row(r) for r in rows]


class SqlUnitOfWork:
    emails: EmailRepository
    poll_state: PollStateRepository
    outgoing: OutgoingRepository
    dsr_messages: DsrMessageRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: MailTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.emails = SqlEmailRepository(self._session, self._tables)
        self.poll_state = SqlPollStateRepository(self._session, self._tables)
        self.outgoing = SqlOutgoingRepository(self._session, self._tables)
        self.dsr_messages = SqlDsrMessageRepository(self._session, self._tables)
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
