"""Liskov check: every UserRepository adapter must pass the same behavioural suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from identity_service.adapters.outbound.memory import InMemoryUserRepository
from identity_service.adapters.outbound.sql import IdentityTables, SqlUserRepository
from identity_service.domain.user import User
from identity_service.ports.repositories import UserRepository
from opspilot_common.auth import Role
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def make(email: str, roles: set[Role], active: bool = True) -> User:
    user = User.register(
        email=email, name=email.split("@")[0], password_hash="h", roles=frozenset(roles), now=NOW
    )
    user.active = active
    return user


@pytest.fixture(params=["memory", "sql"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[UserRepository]:
    if request.param == "memory":
        yield InMemoryUserRepository()
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = IdentityTables(schema=None)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        yield SqlUserRepository(session, tables)
    await engine.dispose()


async def test_add_get_and_lookup_by_email(repo: UserRepository) -> None:
    user = make("lena@example.eu", {Role.L1_ANALYST})
    await repo.add(user)
    assert (await repo.get(user.id)) == user
    assert (await repo.get_by_email("lena@example.eu")) == user
    assert await repo.get("missing") is None
    assert await repo.get_by_email("missing@example.eu") is None


async def test_save_round_trips_every_field(repo: UserRepository) -> None:
    user = make("sam@example.eu", {Role.SUPPORT_AGENT})
    await repo.add(user)
    user.begin_mfa_enrolment("enc", NOW)
    user.accept_mfa_step(42, NOW)
    user.record_failed_attempt(
        NOW, __import__("identity_service.domain.policies").domain.policies.LockoutPolicy()
    )
    user.locked_until = NOW + timedelta(minutes=5)
    user.update_profile(NOW, roles=frozenset({Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER}))
    await repo.save(user)
    assert (await repo.get(user.id)) == user


async def test_count_active_admins(repo: UserRepository) -> None:
    for email, roles, active in [
        ("a@x.eu", {Role.ADMIN}, True),
        ("b@x.eu", {Role.ADMIN, Role.L1_ANALYST}, True),
        ("c@x.eu", {Role.ADMIN}, False),
        ("d@x.eu", {Role.L1_ANALYST}, True),
    ]:
        await repo.add(make(email, roles, active))
    assert await repo.count_active_admins() == 2
    assert len(await repo.list_all()) == 4
