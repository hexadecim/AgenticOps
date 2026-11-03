"""SQLAlchemy adapters. Secrets are stored as one Fernet-encrypted JSON blob per section."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from cryptography.fernet import Fernet
from opspilot_common.events import EventEnvelope, Outbox, inbox_table, outbox_table
from opspilot_common.events.envelope import EventPayload
from opspilot_common.logging import correlation_id
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.document import SettingsDocument
from ...domain.errors import VersionConflictError
from ...ports import EventRecorder, SettingsRepository

SERVICE = "settings-service"


class SettingsTables:
    def __init__(self, schema: str | None = "settings") -> None:
        self.metadata = MetaData(schema=schema)
        self.sections = Table(
            "sections",
            self.metadata,
            Column("section", String(40), primary_key=True),
            Column("version", Integer, nullable=False),
            Column("values", JSON, nullable=False),
            Column("secrets_enc", Text, nullable=True),
            Column("updated_at", DateTime(timezone=True), nullable=True),
            Column("updated_by", String(80), nullable=True),
        )
        self.outbox = outbox_table(self.metadata)
        self.inbox = inbox_table(self.metadata)


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, secrets: dict[str, str]) -> str | None:
        return self._fernet.encrypt(json.dumps(secrets).encode()).decode() if secrets else None

    def open(self, blob: str | None) -> dict[str, str]:
        return json.loads(self._fernet.decrypt(blob.encode())) if blob else {}


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class SqlSettingsRepository:
    def __init__(self, session: AsyncSession, tables: SettingsTables, box: SecretBox) -> None:
        self._s, self._t, self._box = session, tables.sections, box

    async def get(self, section: str) -> SettingsDocument | None:
        row = (await self._s.execute(select(self._t).where(self._t.c.section == section))).first()
        if row is None:
            return None
        return SettingsDocument(
            section=row.section,
            version=row.version,
            values=dict(row.values),
            secrets=self._box.open(row.secrets_enc),
            updated_at=_aware(row.updated_at),
            updated_by=row.updated_by,
        )

    async def save(self, document: SettingsDocument, *, expected_version: int) -> None:
        row: dict[str, Any] = {
            "version": document.version,
            "values": document.values,
            "secrets_enc": self._box.seal(document.secrets),
            "updated_at": document.updated_at,
            "updated_by": document.updated_by,
        }
        if expected_version == 0:
            exists = await self._s.execute(
                select(self._t.c.section).where(self._t.c.section == document.section)
            )
            if exists.first():
                raise VersionConflictError()
            await self._s.execute(insert(self._t).values(section=document.section, **row))
            return
        result = await self._s.execute(
            update(self._t)
            .where(self._t.c.section == document.section, self._t.c.version == expected_version)
            .values(**row)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise VersionConflictError()


class _Recorder:
    def __init__(self) -> None:
        self.pending: list[EventEnvelope] = []

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self.pending.append(
            EventEnvelope.wrap(payload, source=SERVICE, actor=actor, correlation_id=correlation_id.get())
        )


class SqlUnitOfWork:
    settings: SettingsRepository
    events: EventRecorder

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], tables: SettingsTables, box: SecretBox
    ) -> None:
        self._sessions, self._tables, self._box = sessions, tables, box
        self._outbox = Outbox(tables.outbox)

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self.settings = SqlSettingsRepository(self._session, self._tables, self._box)
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
