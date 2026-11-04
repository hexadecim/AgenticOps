"""What SQLite-based tests cannot show: migrations on real Postgres, one role per schema, append-only audit,
pgvector, concurrent relays."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import POSTGRES_HOST, ROOT
from cryptography.fernet import Fernet
from fakeredis.aioredis import FakeRedis
from opspilot_common.events import EventEnvelope, Inbox, Outbox, OutboxRelay
from sqlalchemy import Column, MetaData, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


@dataclass(frozen=True)
class Service:
    folder: str
    package: str
    schema: str
    tables: str

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.schema}_svc:{self.schema}_dev_password@{POSTGRES_HOST}"


SERVICES = [
    Service("identity-service", "identity_service", "identity", "IdentityTables"),
    Service("settings-service", "settings_service", "settings", "SettingsTables"),
    Service("mail-service", "mail_service", "mail", "MailTables"),
    Service("privacy-service", "privacy_service", "privacy", "PrivacyTables"),
    Service("audit-service", "audit_service", "audit", "AuditTables"),
    Service("approval-service", "approval_service", "approval", "ApprovalTables"),
    Service("jira-connector", "jira_connector", "jira", "JiraTables"),
    Service("agent-orchestrator", "agent_orchestrator", "orchestrator", "OrchestratorTables"),
    Service("reporting-service", "reporting_service", "reporting", "ReportingTables"),
    Service("ops-service", "ops_service", "ops", "OpsTables"),
]
BY_SCHEMA = {s.schema: s for s in SERVICES}
_IDS = [s.schema for s in SERVICES]


def alembic(service: Service, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": service.url,
        "JWT_SECRET": "system-test-secret-at-least-32-characters",
        "FERNET_KEY": Fernet.generate_key().decode(),
    }
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT / "services" / service.folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    for service in SERVICES:
        result = alembic(service, "upgrade", "head")
        assert result.returncode == 0, f"{service.folder}: {result.stderr[-2000:]}"


@pytest.fixture
async def engine(request: pytest.FixtureRequest) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(BY_SCHEMA[request.param].url)
    yield created
    await created.dispose()


def _metadata(service: Service) -> MetaData:
    module = importlib.import_module(f"{service.package}.adapters.outbound.sql")
    return getattr(module, service.tables)(schema=service.schema).metadata


@pytest.mark.parametrize("service", SERVICES, ids=_IDS)
async def test_migrations_create_every_table_and_column_the_code_uses(service: Service) -> None:
    engine = create_async_engine(service.url)
    metadata = _metadata(service)

    def actual(connection):  # type: ignore[no-untyped-def]
        found = inspect(connection)
        return {
            t: {c["name"] for c in found.get_columns(t, schema=service.schema)}
            for t in found.get_table_names(schema=service.schema)
        }

    async with engine.connect() as connection:
        columns = await connection.run_sync(actual)
    await engine.dispose()
    assert metadata.tables
    missing = {
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if column.name not in columns.get(table.name, set())
    }
    assert not missing


@pytest.mark.parametrize("service", [s for s in SERVICES if s.schema != "audit"], ids=lambda s: s.schema)
def test_the_latest_migration_can_be_undone_and_redone(service: Service) -> None:
    down = alembic(service, "downgrade", "-1")
    assert down.returncode == 0, down.stderr[-2000:]
    up = alembic(service, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]


@pytest.mark.parametrize("engine", ["mail"], indirect=True)
@pytest.mark.parametrize("other", [s for s in _IDS if s != "mail"])
async def test_a_service_cannot_touch_another_services_schema(engine: AsyncEngine, other: str) -> None:
    table = next(iter(_metadata(BY_SCHEMA[other]).tables.values())).name
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text(f"SELECT 1 FROM {other}.{table}"))


@pytest.mark.parametrize("engine", ["audit"], indirect=True)
@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit.entries SET actor = 'x'", "DELETE FROM audit.entries", "TRUNCATE audit.entries"],
)
async def test_the_audit_log_cannot_be_changed(engine: AsyncEngine, statement: str) -> None:
    table = _metadata(BY_SCHEMA["audit"]).tables["audit.entries"]
    async with engine.begin() as connection:
        await connection.execute(
            table.insert().values(**{c.name: _sample(c) for c in table.columns if not c.nullable})
        )
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(text(statement))


def _sample(column: Column[Any]) -> object:
    python_type = column.type.python_type
    if python_type is datetime:
        return datetime.now(UTC)
    if python_type is dict:
        return {}
    if python_type is int:
        return 1
    return str(uuid.uuid4())[: getattr(column.type, "length", None) or 36]


@pytest.mark.parametrize("engine", ["ops"], indirect=True)
async def test_runbook_search_orders_by_distance_on_pgvector(engine: AsyncEngine) -> None:
    from ops_service.adapters.outbound.sql import OpsTables, SqlRunbookIndex
    from ops_service.domain.model import RunbookChunk

    index = SqlRunbookIndex(async_sessionmaker(engine, expire_on_commit=False), OpsTables(schema="ops"))
    await index.replace(
        "fingerprint",
        [
            (RunbookChunk("far", 0, "h", "t"), [0.0, 1.0, 0.0]),
            (RunbookChunk("near", 0, "h", "t"), [1.0, 0.1, 0.0]),
        ],
    )
    found = await index.nearest([1.0, 0.0, 0.0], 2)
    assert [row[0] for row in found] == ["near", "far"]
    assert found[0][2] > found[1][2]  # similarity, higher is closer


@pytest.mark.parametrize("engine", ["mail"], indirect=True)
async def test_concurrent_relays_publish_each_event_once(engine: AsyncEngine) -> None:
    from mail_service.adapters.outbound.sql import MailTables

    tables = MailTables(schema="mail")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    envelopes = [EventEnvelope(type="system.test", source="test") for _ in range(30)]
    outbox = Outbox(tables.outbox)
    async with sessions() as session, session.begin():
        for envelope in envelopes:
            await outbox.add(session, envelope)

    published: list[str] = []

    class Publisher:
        async def publish(self, envelope: EventEnvelope) -> None:
            await asyncio.sleep(0.001)
            published.append(envelope.id)

    relays = [OutboxRelay(sessions, tables.outbox, Publisher(), batch_size=7) for _ in range(4)]
    wanted = {e.id for e in envelopes}
    for _ in range(20):
        await asyncio.gather(*(r.run_once() for r in relays))
        if wanted <= set(published):
            break
    mine = [p for p in published if p in wanted]
    assert len(mine) == len(wanted), "an event was lost or published twice"


@pytest.mark.parametrize("engine", ["mail"], indirect=True)
async def test_concurrent_deliveries_of_one_event_are_handled_once(engine: AsyncEngine) -> None:
    from mail_service.adapters.outbound.sql import MailTables

    tables = MailTables(schema="mail")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    handled: list[str] = []

    async def handler(_: object, envelope: EventEnvelope) -> None:
        await asyncio.sleep(0.01)
        handled.append(envelope.id)

    guarded = Inbox(tables.inbox, f"system-test-{uuid.uuid4().hex[:8]}").guard(sessions, handler)
    envelope = EventEnvelope(type="system.test", source="test")
    await asyncio.gather(*(guarded(envelope) for _ in range(5)), return_exceptions=True)
    assert handled == [envelope.id]


async def test_stores_answering_an_erasure_at_once_all_count_and_one_confirmation_goes_out() -> None:
    """Five stores acknowledge within a moment. A lost update here would leave the request erasing forever."""
    from opspilot_common.events.catalogue import SubjectStoreErased
    from privacy_service.adapters.outbound.crypto import VaultCrypto
    from privacy_service.adapters.outbound.sql import SqlUnitOfWork
    from privacy_service.application.dsr import RecordStoreErased
    from privacy_service.bootstrap import build_container
    from privacy_service.config import Settings
    from privacy_service.domain.dsr import DataRequest, DsrStatus, DsrType

    engine = create_async_engine(BY_SCHEMA["privacy"].url, pool_size=10)
    container = build_container(
        Settings(jwt_secret="x" * 40, fernet_key=Fernet.generate_key().decode(), log_json=False),
        engine=engine,
        schema="privacy",
        redis=FakeRedis(),
        detector=object(),  # type: ignore[arg-type]
    )
    dsr_id = f"DSR-{uuid.uuid4().hex[:8]}"
    stores = ["mail", "orchestrator", "approval", "jira", "reporting"]
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM privacy.outbox"))
    crypto = VaultCrypto(Fernet.generate_key().decode())
    tables = container.tables

    def uow() -> SqlUnitOfWork:
        return SqlUnitOfWork(container.sessions, tables, crypto)

    async with uow() as unit:
        await unit.requests.add(
            DataRequest(
                dsr_id,
                DsrType.ERASURE,
                DsrStatus.ERASING,
                now.date(),
                now.date(),
                now,
                pending_stores=list(stores),
            )
        )
        await unit.commit()
    record = RecordStoreErased(uow)
    await asyncio.gather(*(record(SubjectStoreErased(dsr_id=dsr_id, store=s, count=1)) for s in stores))
    async with uow() as unit:
        request = await unit.requests.get(dsr_id)
    assert request is not None
    assert request.pending_stores == []
    assert request.status is DsrStatus.CONFIRMING
    async with engine.connect() as connection:
        sent = await connection.scalar(
            text("SELECT count(*) FROM privacy.outbox WHERE type = 'dsr.send_requested'")
        )
    await engine.dispose()
    assert sent == 1


async def test_two_senders_send_a_message_once() -> None:
    """Rule: at most once, also with a second replica of mail-service."""
    from mail_service.adapters.outbound.sql import MailTables, SqlOutgoingRepository
    from mail_service.domain.outgoing import OutgoingMessage

    engine = create_async_engine(BY_SCHEMA["mail"].url, pool_size=10)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tables = MailTables(schema="mail")
    now = datetime.now(UTC)
    message = OutgoingMessage(
        str(uuid.uuid4()), f"m-{uuid.uuid4().hex}", "a", "r", "t", "e", "[EMAIL_1]", None, "s", "b", now, now
    )
    async with sessions() as session, session.begin():
        await SqlOutgoingRepository(session, tables).add(message)

    async def claim() -> bool:
        copy = dataclasses.replace(message)
        async with sessions() as session, session.begin():
            return await SqlOutgoingRepository(session, tables).claim(copy)

    results = await asyncio.gather(*(claim() for _ in range(8)))
    await engine.dispose()
    assert results.count(True) == 1
