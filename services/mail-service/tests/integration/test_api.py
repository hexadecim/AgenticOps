from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from mail_service.adapters.inbound.events import on_settings_changed
from mail_service.adapters.outbound.providers.fake import FakeMailbox
from mail_service.bootstrap import Container, build_container
from mail_service.config import Settings
from mail_service.domain.mail import MailboxConfig
from mail_service.main import create_app
from mail_service.ports import MailboxConnection
from opspilot_common.auth import Principal, Role, ServiceTokenIssuer, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import SettingsChanged
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import NOW, imap_config, raw_mail


class FixedClock:
    """The test mail is dated NOW; the poller's 24 h look-back must be measured from NOW too."""

    def now(self) -> datetime:
        return NOW


SECRET = "mail-test-jwt-secret-at-least-32-chars"


class Source:
    def __init__(self) -> None:
        self.config: MailboxConfig | None = imap_config()

    async def mailbox(self) -> MailboxConfig | None:
        return self.config


class Builder:
    def __init__(self) -> None:
        self.box = FakeMailbox()

    def build(self, config: MailboxConfig) -> MailboxConnection:
        async def close() -> None:
            return None

        return MailboxConnection(config.provider, self.box, self.box, close)


def user(*roles: Role) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + TokenIssuer(SECRET).issue(Principal("u-1", "Lena", frozenset(roles)))
    }


def service(name: str) -> dict[str, str]:
    return ServiceTokenIssuer(SECRET, name).header()


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Container, Builder, Source]]:
    settings = Settings(jwt_secret=SECRET, log_json=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    source, builder = Source(), Builder()
    container = build_container(
        settings,
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        source=source,
        builder=builder,
        clock=FixedClock(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    app = create_app(container, with_events=False, with_scheduler=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mail") as c:
        yield c, container, builder, source
    await engine.dispose()


async def _outbox(container: Container) -> list[dict[str, Any]]:
    async with container.sessions() as s:
        bodies = (await s.execute(select(container.tables.outbox.c.body))).scalars().all()
    return [json.loads(b) for b in bodies]


async def test_poll_stores_mail_and_writes_id_only_events_to_the_outbox(api) -> None:  # type: ignore[no-untyped-def]
    client, container, builder, _ = api
    builder.box.deliver(raw_mail("VPN down", message_id="<root@kunde.de>"), NOW)
    builder.box.deliver(raw_mail("Re: VPN down", in_reply_to="<root@kunde.de>"), NOW)

    assert (await client.post("/v1/poll", headers=user(Role.L1_ANALYST))).status_code == 403
    r = await client.post("/v1/poll", headers=user(Role.ADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["new"] == 2 and r.json()["status"]["level"] == "ok"

    events = await _outbox(container)
    assert [e["type"] for e in events] == ["email.received", "email.received"]
    assert events[0]["payload"]["thread_id"] == events[1]["payload"]["thread_id"]  # reply joined the thread
    assert all("VPN" not in json.dumps(e) and "kunde.de" not in json.dumps(e) for e in events)

    # Second poll and a redelivered copy: still exactly one row each (unique dedupe key in SQL).
    builder.box.deliver(raw_mail("VPN down", message_id="<root@kunde.de>"), NOW)
    assert (await client.post("/v1/poll", headers=user(Role.ADMIN))).json()["new"] == 0
    assert len(await _outbox(container)) == 2

    email_id = events[0]["payload"]["email_id"]
    url = f"/internal/v1/emails/{email_id}"
    assert (await client.get(url, headers=user(Role.ADMIN))).status_code == 403  # people never read raw mail
    assert (await client.get(url, headers=service("jira-connector"))).status_code == 403
    assert (
        await client.get(url, headers=service("agent-orchestrator"))
    ).status_code == 403  # agents: tokens only
    r = await client.get(url, headers=service("privacy-service"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["subject"] == "VPN down" and body["from"]["email"] == "jonas.weber@kunde.de"
    assert body["thread_id"] == events[0]["payload"]["thread_id"]
    r = await client.get("/internal/v1/emails/missing", headers=service("privacy-service"))
    assert r.status_code == 404 and r.json()["code"] == "email_not_found"


async def test_status_for_every_signed_in_role(api) -> None:  # type: ignore[no-untyped-def]
    client, container, _, _ = api
    assert (await client.get("/v1/status")).status_code == 401
    await container.poll()
    r = await client.get("/v1/status", headers=user(Role.COMPLIANCE_LEAD))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True and body["provider"] == "imap" and body["circuit"] == "closed"
    assert body["message"] == "Mailbox polled just now" and body["level"] == "ok"


async def test_settings_changed_event_swaps_the_adapter_once(api) -> None:  # type: ignore[no-untyped-def]
    _, container, _, source = api
    woken: list[bool] = []
    inbox = Inbox(container.tables.inbox, "mail-service.settings")
    handler = inbox.guard(
        container.sessions, on_settings_changed(container.apply_settings, lambda: woken.append(True))
    )
    await container.apply_settings()

    source.config = MailboxConfig(2, "imap", "Support", 300, imap_config(folder="Support").values, {})
    event = EventEnvelope.wrap(SettingsChanged(section="mailbox", version=2), source="settings-service")
    await handler(event)
    await handler(event)  # redelivery: ignored by the inbox
    assert container.active.config is not None and container.active.config.folder == "Support"
    assert woken == [True]

    other = EventEnvelope.wrap(SettingsChanged(section="jira", version=1), source="settings-service")
    await handler(other)
    assert woken == [True]
