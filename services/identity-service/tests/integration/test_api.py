"""End-to-end through FastAPI with a real SQL unit of work (SQLite) and real crypto."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from identity_service.bootstrap import build_container
from identity_service.config import Settings
from identity_service.main import create_app
from opspilot_common.events import EventEnvelope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import FakeClock, totp_code


@pytest.fixture
async def api(settings: Settings, clock: FakeClock) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(settings, engine=engine, clock=clock, schema="")
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    await container.ensure_bootstrap_admin("admin@opspilot.local", "Root-passphrase-77")
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://identity"
    ) as client:
        yield client, container
    await engine.dispose()


async def sign_in(
    client: httpx.AsyncClient,
    clock: FakeClock,
    email: str,
    password: str,
    new_password: str | None = None,
    secret: str | None = None,
) -> tuple[str, str]:
    r = await client.post("/v1/sign-in", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    secret = secret or body["enrolment"]["secret"]
    r = await client.post(
        "/v1/sign-in/mfa",
        json={
            "challenge_token": body["challenge_token"],
            "code": totp_code(secret, clock),
            "new_password": new_password,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["token"], secret


async def test_admin_creates_user_who_signs_in(api, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    client, container = api
    admin_token, _ = await sign_in(client, clock, "admin@opspilot.local", "Root-passphrase-77")
    auth = {"Authorization": f"Bearer {admin_token}"}

    r = await client.post(
        "/v1/users",
        headers=auth,
        json={
            "email": "agent@example.eu",
            "name": "Sam Agent",
            "roles": ["support_agent"],
            "temporary_password": "Temporary-pass-1",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["password_change_required"] is True

    clock.advance(seconds=31)
    token, _ = await sign_in(
        client, clock, "agent@example.eu", "Temporary-pass-1", new_password="Sam-new-passphrase"
    )
    me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["roles"] == ["support_agent"]

    forbidden = await client.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert forbidden.status_code == 403

    users = (await client.get("/v1/users", headers=auth)).json()
    assert [u["email"] for u in users] == ["admin@opspilot.local", "agent@example.eu"]

    async with container.sessions() as s:
        rows = (await s.execute(select(container.tables.outbox.c.body))).scalars().all()
    actions = [EventEnvelope.model_validate_json(b).payload["action"] for b in rows]
    assert actions.count("signed_in") == 2 and "user_created" in actions


async def test_error_contract(api, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    r = await client.post("/v1/sign-in", json={"email": "admin@opspilot.local", "password": "nope"})
    assert r.status_code == 401
    assert r.json() == {"code": "invalid_credentials", "message": "Email or password is incorrect."}
    r = await client.post("/v1/sign-in/mfa", json={"challenge_token": "x", "code": "12345a"})
    assert r.status_code == 422
    assert (await client.get("/v1/me")).status_code == 401


async def test_invalid_email_has_readable_message(api, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    r = await client.post("/v1/sign-in", json={"email": "not-an-email", "password": "x"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["msg"] == "Value error, Enter a valid email address, like name@company.com"
