from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis.aioredis
import httpx
import pytest
from cryptography.fernet import Fernet
from opspilot_common.auth import Principal, Role, TokenIssuer
from settings_service.bootstrap import build_container
from settings_service.config import Settings
from settings_service.main import create_app
from settings_service.ports import Check, ConnectionReport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import IMAP_VALUES

SECRET = "settings-test-jwt-secret-at-least-32-chars"


class OkTester:
    async def test(self, values: dict, secrets: dict) -> ConnectionReport:  # type: ignore[type-arg]
        return ConnectionReport.from_checks([Check("Reach", True, "ok")], f"tested {values.get('imap_host')}")


def bearer(*roles: Role) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + TokenIssuer(SECRET).issue(Principal("u-1", "Admin", frozenset(roles)))
    }


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    settings = Settings(jwt_secret=SECRET, fernet_key=Fernet.generate_key().decode(), log_json=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        settings,
        engine=engine,
        schema="",
        testers={"mailbox:imap": OkTester()},
        redis=fakeredis.aioredis.FakeRedis(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://settings") as c:
        yield c, container
    await engine.dispose()


async def test_admin_only(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    assert (await client.get("/v1/sections")).status_code == 401
    assert (await client.get("/v1/sections", headers=bearer(Role.SUPPORT_MANAGER))).status_code == 403


async def test_save_read_test_and_secrets_never_leave(api) -> None:  # type: ignore[no-untyped-def]
    client, container = api
    admin = bearer(Role.ADMIN)
    sections = (await client.get("/v1/sections", headers=admin)).json()
    assert [s["section"]["id"] for s in sections] == ["mailbox", "jira", "llm"]
    assert sections[2]["values"]["model"] == "deepseek-r1:8b"  # defaults shown before first save

    r = await client.put(
        "/v1/sections/mailbox",
        headers=admin,
        json={"version": 0, "values": IMAP_VALUES, "secrets": {"password": "app-pw-123"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["secrets_set"]["password"] is True
    assert (
        "app-pw-123" not in r.text
        and "app-pw-123" not in (await client.get("/v1/sections/mailbox", headers=admin)).text
    )

    async with container.sessions() as s:
        raw = (await s.execute(select(container.tables.sections.c.secrets_enc))).scalar_one()
    assert "app-pw-123" not in raw  # encrypted at rest

    r = await client.put("/v1/sections/mailbox", headers=admin, json={"version": 0, "values": IMAP_VALUES})
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"

    r = await client.put(
        "/v1/sections/mailbox",
        headers=admin,
        json={"version": 1, "values": {**IMAP_VALUES, "imap_port": "x"}},
    )
    assert r.status_code == 422 and r.json()["errors"] == {"imap_port": "Enter a whole number."}

    r = await client.post(
        "/v1/sections/mailbox/test",
        headers=admin,
        json={"values": {**IMAP_VALUES, "imap_host": "draft.example.com"}},
    )
    assert r.json()["ok"] is True and r.json()["summary"] == "tested draft.example.com"

    async with container.sessions() as s:
        events = (await s.execute(select(container.tables.outbox.c.body))).scalars().all()
    assert len(events) == 1 and "app-pw-123" not in events[0] and '"password"' in events[0]


async def test_unknown_section(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    r = await client.get("/v1/sections/crm", headers=bearer(Role.ADMIN))
    assert r.status_code == 404 and r.json()["code"] == "unknown_section"


async def test_internal_resolved_settings_only_for_the_owning_service(api) -> None:  # type: ignore[no-untyped-def]
    from opspilot_common.auth import ServiceTokenIssuer

    client, _ = api
    await client.put(
        "/v1/sections/mailbox",
        headers=bearer(Role.ADMIN),
        json={"version": 0, "values": IMAP_VALUES, "secrets": {"password": "app-pw-123"}},
    )
    url = "/internal/v1/sections/mailbox/resolved"

    def service(name: str) -> dict[str, str]:
        return ServiceTokenIssuer(SECRET, name).header()

    r = await client.get(url, headers=service("mail-service"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["configured"] is True
    assert body["secrets"] == {"password": "app-pw-123"} and body["values"]["poll_interval_seconds"] == 120
    assert r.headers["cache-control"] == "no-store"

    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=bearer(Role.ADMIN))).status_code == 403  # user session ≠ service
    r = await client.get(url, headers=service("jira-connector"))
    assert r.status_code == 403 and r.json()["code"] == "reader_not_allowed"

    r = await client.get("/internal/v1/sections/jira/resolved", headers=service("jira-connector"))
    assert r.json()["configured"] is False and r.json()["secrets"] == {}
