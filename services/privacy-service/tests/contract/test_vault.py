"""Liskov check: the in-memory and SQL token vaults behave the same; the SQL one never stores plaintext."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from privacy_service.adapters.outbound.crypto import VaultCrypto
from privacy_service.adapters.outbound.memory import InMemoryTokenVault
from privacy_service.adapters.outbound.sql import PrivacyTables, SqlTokenVault
from privacy_service.domain.entities import EntityKind
from privacy_service.ports import TokenVault
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

SESSIONS: dict[str, AsyncSession] = {}


@pytest.fixture(params=["memory", "sql"])
async def vault(request: pytest.FixtureRequest) -> AsyncIterator[TokenVault]:
    if request.param == "memory":
        yield InMemoryTokenVault()
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = PrivacyTables(schema=None)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        SESSIONS["sql"] = session
        yield SqlTokenVault(session, tables, VaultCrypto(Fernet.generate_key().decode()))
    await engine.dispose()


async def test_same_value_same_token_numbered_per_kind(vault: TokenVault) -> None:
    a = await vault.token_for(EntityKind.EMAIL, "Alex.Muster@example.eu", subject=None)
    b = await vault.token_for(EntityKind.EMAIL, "maria@example.eu", subject=None)
    p = await vault.token_for(EntityKind.PERSON, "Alex Muster", subject=a)
    assert (a, b, p) == ("EMAIL_1", "EMAIL_2", "PERSON_1")
    assert await vault.token_for(EntityKind.EMAIL, " alex.muster@EXAMPLE.eu ", subject=None) == a
    assert await vault.token_for(EntityKind.PHONE, "+49 40 1234567", subject=None) == "PHONE_1"
    assert await vault.token_for(EntityKind.PHONE, "0049 (40) 1234567", subject=None) == "PHONE_1"


async def test_values_and_subjects_round_trip(vault: TokenVault) -> None:
    email = await vault.token_for(EntityKind.EMAIL, "alex.muster@example.eu", subject=None)
    name = await vault.token_for(EntityKind.PERSON, "Alex Muster", subject=email)
    other = await vault.token_for(EntityKind.PERSON, "Maria Schmidt", subject=None)
    entry = await vault.get(name)
    assert entry is not None and entry.value == "Alex Muster" and entry.kind is EntityKind.PERSON
    assert sorted(e.value for e in await vault.same_subject(entry)) == [
        "Alex Muster",
        "alex.muster@example.eu",
    ]
    lone = await vault.get(other)
    assert lone is not None and [e.token for e in await vault.same_subject(lone)] == [other]
    assert await vault.get("PERSON_99") is None


async def test_sql_vault_stores_no_plaintext(vault: TokenVault) -> None:
    if not isinstance(vault, SqlTokenVault):
        pytest.skip("SQL only")
    await vault.token_for(EntityKind.EMAIL, "alex.muster@example.eu", subject=None)
    rows = (await SESSIONS["sql"].execute(select(PrivacyTables(schema=None).vault))).all()
    assert rows and all("alex" not in " ".join(str(v) for v in row).lower() for row in rows)
