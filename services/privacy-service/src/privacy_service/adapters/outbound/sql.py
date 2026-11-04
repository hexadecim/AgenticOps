"""SQLAlchemy adapters for schema `privacy`. Vault values are only ever stored encrypted."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from opspilot_common.subjects_http import mentions
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.dsr import DataRequest, DsrStatus, DsrType
from ...domain.entities import EntityKind
from ...domain.records import PseudonymisationRecord, Reveal, TokenisedAddress, TokenisedEmail
from ...domain.tokens import make_token
from ...ports import (
    ErasedEmails,
    EventRecorder,
    RecordRepository,
    RequestRepository,
    RevealRepository,
    TokenVault,
    VaultEntry,
)
from .crypto import VaultCrypto

SERVICE = "privacy-service"


class PrivacyTables:
    def __init__(self, schema: str | None = "privacy") -> None:
        self.metadata = MetaData(schema=schema)
        self.vault = Table(
            "vault",
            self.metadata,
            Column("token", String(40), primary_key=True),
            Column("kind", String(10), nullable=False),
            Column("lookup_hash", String(64), nullable=False),
            Column("value_enc", Text, nullable=False),
            Column("subject", String(40), nullable=False, index=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("last_used_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint("kind", "lookup_hash", name="uq_vault_kind_lookup"),
        )
        self.counters = Table(
            "token_counters",
            self.metadata,
            Column("kind", String(10), primary_key=True),
            Column("last", Integer, nullable=False),
        )
        self.records = Table(
            "pseudonymisations",
            self.metadata,
            Column("id", String(12), primary_key=True),
            Column("email_id", String(36), nullable=False, unique=True),
            Column("thread_id", String(36), nullable=False, index=True),
            Column("counts", JSON, nullable=False),
            Column("signature_removed", Boolean, nullable=False),
            Column("quoted_removed", Boolean, nullable=False),
            Column("email", JSON, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.reveals = Table(
            "reveals",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("token", String(40), nullable=False, index=True),
            Column("user_id", String(36), nullable=False),
            Column("role", String(40), nullable=False),
            Column("reason", Text, nullable=False),
            Column("thread_id", String(36), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("expires_at", DateTime(timezone=True), nullable=False),
        )
        self.requests = Table(
            "data_requests",
            self.metadata,
            Column("id", String(12), primary_key=True),
            Column("type", String(16), nullable=False),
            Column("status", String(12), nullable=False, index=True),
            Column("received_on", Date, nullable=False),
            Column("due_on", Date, nullable=False),
            Column("address_enc", Text, nullable=True),
            Column("subject_token", String(40), nullable=True),
            Column("verified_how", Text, nullable=True),
            Column("verified_at", DateTime(timezone=True), nullable=True),
            Column("pending_stores", JSON, nullable=False),
            Column("erased", JSON, nullable=False),
            Column("attempt", Integer, nullable=False),
            Column("outcome", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("closed_at", DateTime(timezone=True), nullable=True),
        )
        self.erased_emails = Table(
            "erased_emails", self.metadata, Column("email_id", String(36), primary_key=True)
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlTokenVault:
    def __init__(self, session: AsyncSession, tables: PrivacyTables, crypto: VaultCrypto) -> None:
        self._s, self._t, self._c, self._crypto = session, tables.vault, tables.counters, crypto

    async def token_for(self, kind: EntityKind, value: str, *, subject: str | None) -> str:
        lookup = self._crypto.lookup_hash(kind, value)
        found = await self._s.execute(
            select(self._t.c.token).where(self._t.c.kind == kind.value, self._t.c.lookup_hash == lookup)
        )
        if (row := found.first()) is not None:
            await self._s.execute(
                update(self._t).where(self._t.c.token == row.token).values(last_used_at=datetime.now(UTC))
            )
            return str(row.token)
        token = make_token(kind, await self._next_number(kind))
        await self._s.execute(
            insert(self._t).values(
                token=token,
                kind=kind.value,
                lookup_hash=lookup,
                value_enc=self._crypto.seal(value),
                subject=subject or token,
                created_at=datetime.now(UTC),
                last_used_at=datetime.now(UTC),
            )
        )
        return token

    async def _next_number(self, kind: EntityKind) -> int:
        bumped = await self._s.execute(
            update(self._c)
            .where(self._c.c.kind == kind.value)
            .values(last=self._c.c.last + 1)
            .returning(self._c.c.last)
        )
        if (row := bumped.first()) is not None:
            return int(row.last)
        await self._s.execute(insert(self._c).values(kind=kind.value, last=1))
        return 1

    def _entry(self, row: Any) -> VaultEntry:
        return VaultEntry(row.token, EntityKind(row.kind), self._crypto.open(row.value_enc), row.subject)

    async def get(self, token: str) -> VaultEntry | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.token == token))).first()
        return self._entry(row) if row else None

    async def same_subject(self, entry: VaultEntry) -> list[VaultEntry]:
        rows = await self._s.execute(
            select(self._t).where(self._t.c.subject == entry.subject).order_by(self._t.c.created_at)
        )
        return [self._entry(r) for r in rows]

    async def find(self, kind: EntityKind, value: str) -> VaultEntry | None:
        lookup = self._crypto.lookup_hash(kind, value)
        rows = await self._s.execute(
            select(self._t).where(self._t.c.kind == kind.value, self._t.c.lookup_hash == lookup)
        )
        row = rows.first()
        return self._entry(row) if row else None

    async def erase(self, tokens: list[str]) -> int:
        if not tokens:
            return 0
        done = await self._s.execute(delete(self._t).where(self._t.c.token.in_(tokens)))
        return int(done.rowcount)  # type: ignore[attr-defined]

    async def unused_since(self, cutoff: datetime, limit: int) -> list[str]:
        latest = func.max(self._t.c.last_used_at)
        rows = await self._s.execute(
            select(self._t.c.subject).group_by(self._t.c.subject).having(latest < cutoff).limit(limit)
        )
        return [str(r.subject) for r in rows]


def _address_json(a: TokenisedAddress) -> dict[str, str | None]:
    return {"email_token": a.email_token, "name_token": a.name_token}


def _address(d: dict[str, Any]) -> TokenisedAddress:
    return TokenisedAddress(d["email_token"], d.get("name_token"))


def email_to_json(e: TokenisedEmail) -> dict[str, Any]:
    return {
        "email_id": e.email_id,
        "thread_id": e.thread_id,
        "from": _address_json(e.sender),
        "to": [_address_json(a) for a in e.to],
        "cc": [_address_json(a) for a in e.cc],
        "subject": e.subject,
        "body": e.body,
        "received_at": e.received_at.isoformat(),
        "language": e.language,
        "attachments": list(e.attachments),
    }


def email_from_json(d: dict[str, Any]) -> TokenisedEmail:
    return TokenisedEmail(
        email_id=d["email_id"],
        thread_id=d["thread_id"],
        sender=_address(d["from"]),
        to=tuple(_address(a) for a in d["to"]),
        cc=tuple(_address(a) for a in d["cc"]),
        subject=d["subject"],
        body=d["body"],
        received_at=_aware(datetime.fromisoformat(d["received_at"])),
        language=d["language"],
        attachments=tuple(d.get("attachments", [])),
    )


class SqlRecordRepository:
    def __init__(self, session: AsyncSession, tables: PrivacyTables) -> None:
        self._s, self._t = session, tables.records

    async def by_email(self, email_id: str) -> PseudonymisationRecord | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.email_id == email_id))).first()
        return self._record(row) if row is not None else None

    def _record(self, row: Any) -> PseudonymisationRecord:
        return PseudonymisationRecord(
            row.id,
            row.email_id,
            row.thread_id,
            dict(row.counts),
            row.signature_removed,
            row.quoted_removed,
            email_from_json(row.email),
            _aware(row.created_at),
        )

    async def add(self, record: PseudonymisationRecord) -> None:
        await self._s.execute(
            insert(self._t).values(
                id=record.id,
                email_id=record.email_id,
                thread_id=record.thread_id,
                counts=record.counts,
                signature_removed=record.signature_removed,
                quoted_removed=record.quoted_removed,
                email=email_to_json(record.email),
                created_at=record.created_at,
            )
        )

    async def mentioning(self, tokens: list[str]) -> list[PseudonymisationRecord]:
        rows = await self._s.execute(select(self._t).where(mentions(self._t.c.email, tokens)))
        return [self._record(row) for row in rows]

    async def delete_emails(self, email_ids: list[str]) -> int:
        if not email_ids:
            return 0
        done = await self._s.execute(delete(self._t).where(self._t.c.email_id.in_(email_ids)))
        return int(done.rowcount)  # type: ignore[attr-defined]


class SqlRequestRepository:
    def __init__(self, session: AsyncSession, tables: PrivacyTables, crypto: VaultCrypto) -> None:
        self._s, self._t, self._c, self._crypto = session, tables.requests, tables.counters, crypto

    def _values(self, r: DataRequest) -> dict[str, Any]:
        return {
            "type": r.type.value,
            "status": r.status.value,
            "received_on": r.received_on,
            "due_on": r.due_on,
            "address_enc": self._crypto.seal(r.address) if r.address else None,
            "subject_token": r.subject_token,
            "verified_how": r.verified_how,
            "verified_at": r.verified_at,
            "pending_stores": r.pending_stores,
            "erased": r.erased,
            "attempt": r.attempt,
            "outcome": r.outcome,
            "created_at": r.created_at,
            "closed_at": r.closed_at,
        }

    def _request(self, row: Any) -> DataRequest:
        received: date = row.received_on
        return DataRequest(
            row.id, DsrType(row.type), DsrStatus(row.status), received, row.due_on, _aware(row.created_at),
            address=self._crypto.open(row.address_enc) if row.address_enc else None,
            subject_token=row.subject_token, verified_how=row.verified_how,
            verified_at=_aware(row.verified_at) if row.verified_at else None,
            pending_stores=list(row.pending_stores),
            erased=dict(row.erased), attempt=row.attempt, outcome=row.outcome,
            closed_at=_aware(row.closed_at) if row.closed_at else None,
        )  # fmt: skip

    async def next_number(self) -> int:
        bumped = await self._s.execute(
            update(self._c)
            .where(self._c.c.kind == "DSR")
            .values(last=self._c.c.last + 1)
            .returning(self._c.c.last)
        )
        if (row := bumped.first()) is not None:
            return int(row.last)
        await self._s.execute(insert(self._c).values(kind="DSR", last=1))
        return 1

    async def add(self, request: DataRequest) -> None:
        await self._s.execute(insert(self._t).values(id=request.id, **self._values(request)))

    async def save(self, request: DataRequest) -> None:
        await self._s.execute(
            update(self._t).where(self._t.c.id == request.id).values(**self._values(request))
        )

    async def get(self, dsr_id: str) -> DataRequest | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.id == dsr_id))).first()
        return self._request(row) if row else None

    async def all(self) -> list[DataRequest]:
        rows = await self._s.execute(select(self._t).order_by(self._t.c.id.desc()).limit(200))
        return [self._request(r) for r in rows]


class SqlErasedEmails:
    def __init__(self, session: AsyncSession, tables: PrivacyTables) -> None:
        self._s, self._t = session, tables.erased_emails

    async def add(self, email_ids: list[str]) -> None:
        for email_id in email_ids:
            if not await self.contains(email_id):
                await self._s.execute(insert(self._t).values(email_id=email_id))

    async def contains(self, email_id: str) -> bool:
        return (
            await self._s.execute(select(self._t).where(self._t.c.email_id == email_id))
        ).first() is not None


class SqlRevealRepository:
    def __init__(self, session: AsyncSession, tables: PrivacyTables) -> None:
        self._s, self._t = session, tables.reveals

    async def add(self, reveal: Reveal) -> None:
        await self._s.execute(
            insert(self._t).values(
                id=reveal.id,
                token=reveal.token,
                user_id=reveal.user_id,
                role=reveal.role,
                reason=reveal.reason,
                thread_id=reveal.thread_id,
                created_at=reveal.created_at,
                expires_at=reveal.expires_at,
            )
        )


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    vault: TokenVault
    records: RecordRepository
    reveals: RevealRepository
    requests: RequestRepository
    erased: ErasedEmails
    events: EventRecorder

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], tables: PrivacyTables, crypto: VaultCrypto
    ) -> None:
        self._sessions, self._tables, self._crypto = sessions, tables, crypto
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.vault = SqlTokenVault(self._session, self._tables, self._crypto)
        self.records = SqlRecordRepository(self._session, self._tables)
        self.reveals = SqlRevealRepository(self._session, self._tables)
        self.requests = SqlRequestRepository(self._session, self._tables, self._crypto)
        self.erased = SqlErasedEmails(self._session, self._tables)
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
