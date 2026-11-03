"""SQLAlchemy adapters: user repository and unit of work (state + outbox in one transaction)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from opspilot_common.auth import Role
from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.user import User
from ...ports.repositories import EventRecorder, UserRepository

SERVICE = "identity-service"


class IdentityTables:
    def __init__(self, schema: str | None = "identity") -> None:
        self.metadata = MetaData(schema=schema)
        self.users = Table(
            "users",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("email", String(320), nullable=False, unique=True),
            Column("name", String(200), nullable=False),
            Column("password_hash", Text, nullable=False),
            Column("roles", JSON, nullable=False),
            Column("active", Boolean, nullable=False),
            Column("password_change_required", Boolean, nullable=False),
            Column("mfa_secret_enc", Text, nullable=True),
            Column("mfa_enrolled", Boolean, nullable=False),
            Column("mfa_last_step", Integer, nullable=False),
            Column("failed_logins", Integer, nullable=False),
            Column("locked_until", DateTime(timezone=True), nullable=True),
            Column("last_login_at", DateTime(timezone=True), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=True),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; everything in the domain is UTC-aware."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _to_row(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "password_hash": user.password_hash,
        "roles": sorted(r.value for r in user.roles),
        "active": user.active,
        "password_change_required": user.password_change_required,
        "mfa_secret_enc": user.mfa_secret_enc,
        "mfa_enrolled": user.mfa_enrolled,
        "mfa_last_step": user.mfa_last_step,
        "failed_logins": user.failed_logins,
        "locked_until": user.locked_until,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _from_row(row: Any) -> User:
    return User(
        id=row.id,
        email=row.email,
        name=row.name,
        password_hash=row.password_hash,
        roles=frozenset(Role(r) for r in row.roles),
        active=row.active,
        password_change_required=row.password_change_required,
        mfa_secret_enc=row.mfa_secret_enc,
        mfa_enrolled=row.mfa_enrolled,
        mfa_last_step=row.mfa_last_step,
        failed_logins=row.failed_logins,
        locked_until=_aware(row.locked_until),
        last_login_at=_aware(row.last_login_at),
        created_at=_aware(row.created_at) or datetime.now(UTC),
        updated_at=_aware(row.updated_at),
    )


class SqlUserRepository:
    def __init__(self, session: AsyncSession, tables: IdentityTables) -> None:
        self._session, self._t = session, tables.users

    async def get(self, user_id: str) -> User | None:
        row = (await self._session.execute(select(self._t).where(self._t.c.id == user_id))).first()
        return _from_row(row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        row = (await self._session.execute(select(self._t).where(self._t.c.email == email))).first()
        return _from_row(row) if row else None

    async def add(self, user: User) -> None:
        await self._session.execute(self._t.insert().values(**_to_row(user)))

    async def save(self, user: User) -> None:
        values = _to_row(user)
        values.pop("id")
        await self._session.execute(self._t.update().where(self._t.c.id == user.id).values(**values))

    async def list_all(self) -> list[User]:
        return [_from_row(r) for r in (await self._session.execute(select(self._t))).all()]

    async def count_active_admins(self) -> int:
        # roles is JSON for portability; the user table is small, so count in Python.
        rows = (await self._session.execute(select(self._t.c.roles).where(self._t.c.active.is_(True)))).all()
        return sum(1 for r in rows if Role.ADMIN.value in r.roles)


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    users: UserRepository
    events: EventRecorder

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: IdentityTables) -> None:
        self._sessions, self._tables = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.users = SqlUserRepository(self._session, self._tables)
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


__all__ = ["IdentityTables", "SqlUnitOfWork", "SqlUserRepository"]
