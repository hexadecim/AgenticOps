from __future__ import annotations

import json
from collections.abc import AsyncIterator

import fakeredis.aioredis
import httpx
import pytest
from cryptography.fernet import Fernet
from opspilot_common.auth import Principal, Role, ServiceTokenIssuer, TokenIssuer
from privacy_service.adapters.outbound.detectors import CompositeDetector
from privacy_service.adapters.outbound.detectors.regex import RegexDetector
from privacy_service.bootstrap import Container, build_container
from privacy_service.config import Settings
from privacy_service.main import create_app
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import BODY, FakeNer, FakeSource, raw_email

SECRET = "privacy-test-jwt-secret-at-least-32-chars"


def service(name: str) -> dict[str, str]:
    return ServiceTokenIssuer(SECRET, name).header()


def user(*roles: Role) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + TokenIssuer(SECRET).issue(Principal("u-7", "Lena", frozenset(roles)))
    }


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Container, FakeSource]]:
    settings = Settings(jwt_secret=SECRET, fernet_key=Fernet.generate_key().decode(), log_json=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    source = FakeSource(raw_email())
    container = build_container(
        settings,
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        source=source,
        detector=CompositeDetector([RegexDetector(), FakeNer("Maria Schmidt")]),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://privacy") as c:
        yield c, container, source
    await engine.dispose()


async def test_pseudonymise_reveal_detokenise_end_to_end(api) -> None:  # type: ignore[no-untyped-def]
    client, container, _ = api
    url = "/internal/v1/emails/e-1/pseudonymise"
    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers=user(Role.ADMIN))).status_code == 403  # people cannot call it
    assert (await client.post(url, headers=service("mail-service"))).status_code == 403
    r = await client.post(url, headers=service("agent-orchestrator"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"]["from"] == {"email_token": "EMAIL_1", "name_token": "PERSON_1"}
    assert "[PHONE_1]" in body["email"]["body"] and body["summary"].startswith(
        "EMAIL (2), PERSON (2), PHONE (1)"
    )
    for raw in ("Alex", "Muster", "example.eu", "1234567", "Maria", "Hafenstraße"):
        assert raw not in r.text

    async with container.sessions() as s:
        vault = " ".join(
            str(v) for row in (await s.execute(select(container.tables.vault))).all() for v in row
        )
        events = (await s.execute(select(container.tables.outbox.c.body))).scalars().all()
    assert "Alex" not in vault and "example.eu" not in vault  # encrypted at rest
    assert len(events) == 1 and json.loads(events[0])["payload"]["action"] == "pseudonymise"

    r = await client.post(
        "/v1/reveals",
        headers=user(Role.L1_ANALYST),
        json={"token": "[PERSON_1]", "reason": "Verify the customer's account", "thread_id": "thr-0001-aaaa"},
    )
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["values"] == {"EMAIL": "alex.muster@example.eu", "PERSON": "Alex Muster"}
    r = await client.post(
        "/v1/reveals", headers=user(Role.ADMIN), json={"token": "PERSON_1", "reason": "curious"}
    )
    assert r.status_code == 403 and r.json()["code"] == "not_allowed"

    r = await client.post(
        "/internal/v1/detokenise",
        headers=service("mail-service"),
        json={"text": "Dear [PERSON_1]", "purpose": "reply"},
    )
    assert r.json() == {"text": "Dear Alex Muster"}
    r = await client.post(
        "/internal/v1/detokenise",
        headers=service("agent-orchestrator"),
        json={"text": "[PERSON_1]", "purpose": "x"},
    )
    assert r.status_code == 403


async def test_upstream_outage_fails_closed_without_any_content(api) -> None:  # type: ignore[no-untyped-def]
    client, _, source = api
    source.down = True
    r = await client.post("/internal/v1/emails/e-1/pseudonymise", headers=service("agent-orchestrator"))
    assert (
        r.status_code == 503
        and r.json()["code"] == "privacy_unavailable"
        and r.headers["retry-after"] == "30"
    )
    assert BODY[:20] not in r.text and "Alex" not in r.text
    r = await client.post("/internal/v1/emails/missing/pseudonymise", headers=service("agent-orchestrator"))
    assert r.status_code == 503  # still down
    source.down = False
    r = await client.post("/internal/v1/emails/missing/pseudonymise", headers=service("agent-orchestrator"))
    assert r.status_code == 404 and r.json()["code"] == "email_not_found"
