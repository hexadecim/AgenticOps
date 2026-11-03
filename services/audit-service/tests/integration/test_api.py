from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import httpx
import opspilot_common.events.catalogue as c
import pytest
from audit_service.adapters.inbound.events import on_any_event
from audit_service.bootstrap import Container, build_container
from audit_service.config import Settings
from audit_service.main import create_app
from opspilot_common.auth import Principal, Role, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

SECRET = "audit-test-jwt-secret-at-least-32-chars"
T0 = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)


def user(*roles: Role) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + TokenIssuer(SECRET).issue(Principal("u-1", "Lena", frozenset(roles)))
    }


def at(minutes: int, payload: c.EventPayload, actor: str | None = None) -> EventEnvelope:
    env = EventEnvelope.wrap(payload, source="test", actor=actor)
    return env.model_copy(update={"occurred_at": T0 + timedelta(minutes=minutes)})


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Container]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_container(
        Settings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    handler = Inbox(container.tables.inbox, "audit-service.all").guard(
        container.sessions, on_any_event(container.record, container.tables)
    )
    events = [
        at(0, c.UserActivity(user_id="u-1", action="signed_in", actor_role="l1_analyst"), "u-1"),
        at(1, c.EmailReceived(email_id="e", thread_id="thr-1", provider="imap", received_at=T0)),
        at(
            2,
            c.PrivacyRecorded(
                action="pseudonymise", detail="PERSON (1) tokenised", record_id="vlt-1", thread_id="thr-1"
            ),
        ),
        at(
            3,
            c.PrivacyRecorded(
                action="reveal_identity",
                detail="Viewed identity of [PERSON_1] for 5 min: verify",
                subject_token="PERSON_1",
                actor_role="l1_analyst",
            ),
            "u-1",
        ),
        at(4, c.SettingsChanged(section="mailbox", version=2, changed_fields=["imap_host"]), "u-9"),
    ]
    for event in events:
        await handler(event)
    await handler(events[0])  # redelivered: ignored
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://audit") as client:
        yield client, container
    await engine.dispose()


async def test_every_event_once_newest_first_with_filters_and_paging(api) -> None:  # type: ignore[no-untyped-def]
    client, container = api
    async with container.sessions() as s:
        assert (await s.execute(select(func.count()).select_from(container.tables.entries))).scalar() == 5

    reader = user(Role.COMPLIANCE_LEAD)
    body = (await client.get("/v1/entries", headers=reader)).json()
    assert [e["event"] for e in body["entries"]] == [
        "change_settings", "reveal_identity", "pseudonymise", "receive_email", "signed_in",
    ]  # fmt: skip
    assert body["entries"][-1]["actor"] == "L1 Analyst" and body["next_cursor"] is None

    privacy = (await client.get("/v1/entries", params={"kind": "privacy"}, headers=reader)).json()["entries"]
    assert [e["event"] for e in privacy] == ["reveal_identity", "pseudonymise"]
    people = (await client.get("/v1/entries", params={"kind": "human"}, headers=reader)).json()["entries"]
    assert [e["event"] for e in people] == ["change_settings", "signed_in"]
    found = (await client.get("/v1/entries", params={"q": "[PERSON_1]"}, headers=reader)).json()["entries"]
    assert [e["event"] for e in found] == ["reveal_identity"]
    by_thread = (await client.get("/v1/entries", params={"q": "thr-1"}, headers=reader)).json()["entries"]
    assert len(by_thread) == 2

    page1 = (await client.get("/v1/entries", params={"limit": 2}, headers=reader)).json()
    page2 = (
        await client.get("/v1/entries", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=reader)
    ).json()
    page3 = (
        await client.get("/v1/entries", params={"limit": 2, "cursor": page2["next_cursor"]}, headers=reader)
    ).json()
    seen = [e["event"] for p in (page1, page2, page3) for e in p["entries"]]
    assert len(seen) == 5 and len(set(seen)) == 5 and page3["next_cursor"] is None


async def test_only_audit_readers(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    assert (await client.get("/v1/entries")).status_code == 401
    r = await client.get("/v1/entries", headers=user(Role.L1_ANALYST))
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    for role in (Role.SUPPORT_MANAGER, Role.ADMIN):
        assert (await client.get("/v1/entries", headers=user(role))).status_code == 200
    assert (
        await client.get("/v1/entries", params={"cursor": "garbage!"}, headers=user(Role.ADMIN))
    ).status_code == 422
