"""Data requests through the API on a real SQL vault, with the other services' stores faked."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import fakeredis.aioredis
import httpx
import opspilot_common.events.catalogue as c
import pytest
from cryptography.fernet import Fernet
from opspilot_common.auth import Principal, Role, ServiceTokenIssuer, TokenIssuer
from opspilot_common.subjects import StoreReport, SubjectQuery
from privacy_service.adapters.outbound.detectors import CompositeDetector
from privacy_service.adapters.outbound.detectors.regex import RegexDetector
from privacy_service.bootstrap import Container, build_container
from privacy_service.config import Settings
from privacy_service.domain.dsr import add_month
from privacy_service.main import create_app
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from ..conftest import FakeNer, FakeSource, raw_email

SECRET = "privacy-test-jwt-secret-at-least-32-chars"
ADDRESS = "alex.muster@example.eu"
STORES = ("mail", "orchestrator", "approval", "jira", "reporting")


def service(name: str) -> dict[str, str]:
    return ServiceTokenIssuer(SECRET, name).header()


def user(*roles: Role) -> dict[str, str]:
    principal = Principal("u-7", "Lena", frozenset(roles))
    return {"Authorization": "Bearer " + TokenIssuer(SECRET).issue(principal)}


LEAD = user(Role.COMPLIANCE_LEAD)


class FakeStores:
    def __init__(self) -> None:
        self.down: set[str] = set()
        self.busy = 0
        self.open = 0
        self.queries: list[SubjectQuery] = []

    async def gather(self, query: SubjectQuery) -> list[StoreReport]:
        self.queries.append(query)
        return [
            StoreReport(
                store="mail",
                label="Mail store",
                what="emails",
                count=2,
                available="mail" not in self.down,
                email_ids=["e-1", "e-untokenised"],
                items=[{"subject": "raw"}] if query.include_items else [],
            ),
            StoreReport(
                store="orchestrator",
                label="Thread checkpoints",
                what="runs",
                count=1,
                run_ids=["run-1"],
                open_runs=self.open,
                busy_runs=self.busy,
            ),
        ]


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Container, FakeStores]]:
    settings = Settings(jwt_secret=SECRET, fernet_key=Fernet.generate_key().decode(), log_json=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    stores = FakeStores()
    container = build_container(
        settings,
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        source=FakeSource(raw_email(), raw_email("e-2", sender=raw_email().to[0])),
        detector=CompositeDetector([RegexDetector(), FakeNer("Maria Schmidt")]),
        stores=stores,
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    app = create_app(container, with_events=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://privacy") as client:
        await client.post("/internal/v1/emails/e-1/pseudonymise", headers=service("agent-orchestrator"))
        yield client, container, stores
    await engine.dispose()


async def _log(client: httpx.AsyncClient, kind: str, address: str = ADDRESS) -> str:
    r = await client.post("/v1/dsr", headers=LEAD, json={"type": kind, "address": address})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _verify(client: httpx.AsyncClient, dsr_id: str) -> None:
    r = await client.post(f"/v1/dsr/{dsr_id}/verify", headers=LEAD, json={"how": "Replied from the address"})
    assert r.status_code == 200, r.text


async def _events(container: Container) -> list[c.EventPayload]:
    from opspilot_common.events import EventEnvelope, parse_payload

    async with container.sessions() as s:
        rows = await s.execute(select(container.tables.outbox.c.body))
        return [parse_payload(EventEnvelope.model_validate_json(r.body)) for r in rows]


def test_one_calendar_month_clamps_to_the_shorter_month() -> None:
    assert add_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert add_month(date(2026, 12, 15)) == date(2027, 1, 15)


async def test_only_the_compliance_lead_may_handle_requests(api) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = api
    for role in (Role.ADMIN, Role.SUPPORT_MANAGER, Role.L1_ANALYST):
        assert (await client.get("/v1/dsr", headers=user(role))).status_code == 403
    body = {"type": "access", "address": ADDRESS}
    assert (await client.post("/v1/dsr", headers=user(Role.ADMIN), json=body)).status_code == 403
    assert (await client.get("/v1/dsr")).status_code == 401


async def test_the_api_shows_the_token_and_never_the_address(api) -> None:  # type: ignore[no-untyped-def]
    client, container, _ = api
    dsr_id = await _log(client, "access", ADDRESS.upper())  # any case finds the same person
    assert (await container.get_request(Principal("u", "L", frozenset({Role.COMPLIANCE_LEAD})), dsr_id))[
        0
    ].address == ADDRESS
    listed = await client.get("/v1/dsr", headers=LEAD)
    detail = await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)
    assert detail.json()["who"] == "[EMAIL_1]" and detail.json()["article"].startswith("Art. 15")
    assert listed.json()["open"] == 1 and detail.json()["due_level"] == "ok"
    for text in (listed.text, detail.text):
        assert "example.eu" not in text.lower()
    async with container.sessions() as s:
        stored = (await s.execute(select(container.tables.requests.c.address_enc))).scalar_one()
    assert "example.eu" not in stored
    for event in await _events(container):
        assert "example.eu" not in event.model_dump_json().lower()


async def test_export_needs_verification_and_goes_only_to_the_stored_address(api) -> None:  # type: ignore[no-untyped-def]
    client, container, stores = api
    dsr_id = await _log(client, "access")
    outbound = f"/internal/v1/dsr/{dsr_id}/outbound?kind=export"
    assert (await client.post(f"/v1/dsr/{dsr_id}/export", headers=LEAD)).status_code == 403
    assert (await client.get(outbound, headers=service("mail-service"))).status_code == 403  # not decided

    await _verify(client, dsr_id)
    stores.down = {"mail"}
    assert (await client.post(f"/v1/dsr/{dsr_id}/export", headers=LEAD)).status_code == 409  # never partial
    stores.down = set()
    assert (await client.post(f"/v1/dsr/{dsr_id}/export", headers=LEAD)).json()["status"] == "sending"
    assert any(isinstance(e, c.DsrSendRequested) and e.kind == "export" for e in await _events(container))

    assert (await client.get(outbound, headers=service("agent-orchestrator"))).status_code == 403
    r = await client.get(outbound, headers=service("mail-service"))
    assert r.status_code == 200 and r.json()["to"] == ADDRESS and r.headers["cache-control"] == "no-store"
    package = json.loads(r.json()["attachment"])
    assert {"kind": "email", "value": ADDRESS} in package["about_you"]
    assert "[EMAIL_1]" not in package["notes"]
    assert package["stores"]["mail"]["items"] == [{"subject": "raw"}]
    assert package["stores"]["records"]["count"] == 1
    assert "Maria" not in r.text  # someone else named in their email stays a token

    handled = container.record_dsr_message
    await handled(c.DsrMessageResult(dsr_id=dsr_id, kind="export", sent=True))
    done = (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()
    assert done["status"] == "completed" and done["closed_at"]
    async with container.sessions() as s:
        assert (await s.execute(select(container.tables.requests.c.address_enc))).scalar_one() is None
    assert (await client.get(outbound, headers=service("mail-service"))).status_code == 403


async def test_failed_send_is_visible_and_can_be_retried(api) -> None:  # type: ignore[no-untyped-def]
    client, container, _ = api
    dsr_id = await _log(client, "access")
    await _verify(client, dsr_id)
    await client.post(f"/v1/dsr/{dsr_id}/export", headers=LEAD)
    await container.record_dsr_message(
        c.DsrMessageResult(dsr_id=dsr_id, kind="export", sent=False, detail="No")
    )
    failed = (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()
    assert failed["status"] == "send_failed" and failed["outcome"] == "No"
    assert (await client.post(f"/v1/dsr/{dsr_id}/retry", headers=LEAD)).json()["status"] == "sending"
    # A late result for attempt 1 does not close attempt 2.
    await container.record_dsr_message(c.DsrMessageResult(dsr_id=dsr_id, kind="export", sent=True, attempt=1))
    assert (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()["status"] == "sending"
    await container.record_dsr_message(c.DsrMessageResult(dsr_id=dsr_id, kind="export", sent=True, attempt=2))
    assert (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()["status"] == "completed"


async def test_erasure_kills_the_tokens_then_confirms_after_every_store(api) -> None:  # type: ignore[no-untyped-def]
    client, container, stores = api
    dsr_id = await _log(client, "erasure")
    assert (await client.post(f"/v1/dsr/{dsr_id}/erase", headers=LEAD)).status_code == 403  # not verified
    await _verify(client, dsr_id)
    stores.busy = 1
    assert (await client.post(f"/v1/dsr/{dsr_id}/erase", headers=LEAD)).status_code == 409
    stores.busy = 0
    stores.down = {"mail"}
    assert (await client.post(f"/v1/dsr/{dsr_id}/erase", headers=LEAD)).status_code == 409
    assert (
        await client.post(
            "/v1/reveals",
            headers=user(Role.COMPLIANCE_LEAD),
            json={"token": "[PERSON_1]", "reason": "Still here?"},
        )
    ).status_code == 200  # nothing was changed by the refusals
    stores.down = set()

    r = await client.post(f"/v1/dsr/{dsr_id}/erase", headers=LEAD)
    assert r.status_code == 200 and r.json()["status"] == "erasing"
    assert r.json()["pending_stores"] == list(STORES)
    request = next(e for e in await _events(container) if isinstance(e, c.SubjectErasureRequested))
    assert set(request.tokens) >= {"EMAIL_1", "PERSON_1"}
    assert set(request.own_email_ids) == {"e-1", "e-untokenised"} and request.run_ids == ["run-1"]

    body = {"token": "[PERSON_1]", "reason": "Is it gone?"}
    assert (await client.post("/v1/reveals", headers=LEAD, json=body)).status_code == 404
    send = {"text": "[EMAIL_1]", "purpose": "customer reply"}
    r = await client.post("/internal/v1/detokenise", headers=service("mail-service"), json=send)
    assert r.status_code == 404
    async with container.sessions() as s:
        assert (await s.execute(select(container.tables.records))).first() is None
    # A run still queued cannot bring the person back.
    again = "/internal/v1/emails/e-1/pseudonymise"
    assert (await client.post(again, headers=service("agent-orchestrator"))).status_code == 404

    confirm = f"/internal/v1/dsr/{dsr_id}/outbound?kind=erasure_confirmation"
    assert (await client.get(confirm, headers=service("mail-service"))).status_code == 403  # stores not done
    for store in STORES:
        await container.record_store_erased(c.SubjectStoreErased(dsr_id=dsr_id, store=store, count=1))
        await container.record_store_erased(
            c.SubjectStoreErased(dsr_id=dsr_id, store=store, count=1)
        )  # repeat
    sends = [e for e in await _events(container) if isinstance(e, c.DsrSendRequested)]
    assert [e.kind for e in sends] == ["erasure_confirmation"]
    r = await client.get(confirm, headers=service("mail-service"))
    assert r.status_code == 200 and r.json()["to"] == ADDRESS and not r.json()["attachment"]

    await container.record_dsr_message(c.DsrMessageResult(dsr_id=dsr_id, kind=sends[0].kind, sent=True))
    done = (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()
    assert done["status"] == "completed" and done["who"] == "[EMAIL_1]" and "Erased" in done["outcome"]
    assert {s["store"] for s in done["stores"]} >= {"vault", "records"}
    assert next(s for s in done["stores"] if s["store"] == "vault")["count"] == 0
    # The screen keeps showing what stays: the audit log is still asked, by the erased tokens and ids.
    asked = stores.queries[-1]
    assert set(asked.tokens) >= {"EMAIL_1", "PERSON_1"} and asked.run_ids == ["run-1"]
    async with container.sessions() as s:
        assert (await s.execute(select(container.tables.requests.c.address_enc))).scalar_one() is None


async def test_after_erasure_the_address_is_unknown_again(api) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = api
    dsr_id = await _log(client, "erasure")
    await _verify(client, dsr_id)
    await client.post(f"/v1/dsr/{dsr_id}/erase", headers=LEAD)
    fresh = await _log(client, "access")
    assert (await client.get(f"/v1/dsr/{fresh}", headers=LEAD)).json()["who"] is None


async def test_unknown_address_is_answered_without_inventing_a_person(api) -> None:  # type: ignore[no-untyped-def]
    client, container, stores = api
    dsr_id = await _log(client, "access", "nobody@example.org")
    assert (await client.get(f"/v1/dsr/{dsr_id}", headers=LEAD)).json()["who"] is None
    assert stores.queries[-1].tokens == [] and stores.queries[-1].address == "nobody@example.org"
    await _verify(client, dsr_id)
    await client.post(f"/v1/dsr/{dsr_id}/export", headers=LEAD)
    r = await client.get(f"/internal/v1/dsr/{dsr_id}/outbound?kind=export", headers=service("mail-service"))
    assert r.json()["to"] == "nobody@example.org" and json.loads(r.json()["attachment"])["about_you"] == []
    async with container.sessions() as s:
        assert len(list(await s.execute(select(container.tables.vault)))) > 0


async def test_other_types_close_by_hand_with_a_recorded_outcome(api) -> None:  # type: ignore[no-untyped-def]
    client, container, _ = api
    dsr_id = await _log(client, "objection")
    assert (
        await client.post(f"/v1/dsr/{dsr_id}/close", headers=LEAD, json={"outcome": "no"})
    ).status_code == 422
    r = await client.post(
        f"/v1/dsr/{dsr_id}/close", headers=LEAD, json={"outcome": "Stopped processing by hand"}
    )
    assert r.json()["status"] == "completed"
    audited = [
        e for e in await _events(container) if isinstance(e, c.PrivacyRecorded) and e.action == "dsr_closed"
    ]
    assert audited[0].actor_role == "compliance_lead" and "Art. 21" in (audited[0].legal_basis or "")
    assert (
        await client.post(f"/v1/dsr/{dsr_id}/close", headers=LEAD, json={"outcome": "Again now"})
    ).status_code == 422


async def test_retention_purges_only_long_unused_people_with_no_open_run(api) -> None:  # type: ignore[no-untyped-def]
    _, container, stores = api
    t = container.tables.vault

    async def age(days: int) -> None:
        async with container.sessions() as s, s.begin():
            await s.execute(update(t).values(last_used_at=datetime.now(UTC) - timedelta(days=days)))

    async def people() -> int:
        async with container.sessions() as s:
            return len(list(await s.execute(select(t).where(t.c.token == "EMAIL_1"))))

    await age(5)
    assert await container.purge_identities() == 0 and await people() == 1
    await age(40)
    stores.open = 1
    assert await container.purge_identities() == 0 and await people() == 1  # a reply may yet go out
    stores.open, stores.down = 0, {"mail"}
    assert await container.purge_identities() == 0 and await people() == 1  # cannot tell: keep
    stores.down = set()
    assert await container.purge_identities() >= 1 and await people() == 0
    audited = [e for e in await _events(container) if isinstance(e, c.PrivacyRecorded)]
    assert any(
        e.action == "retention_purge" and "storage limitation" in (e.legal_basis or "") for e in audited
    )


async def test_schedule_says_what_really_happens(api) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = api
    keep = {r["what"]: r["keep"] for r in (await client.get("/v1/dsr", headers=LEAD)).json()["retention"]}
    assert "job off" in keep["Token vault (real names and addresses)"]
    assert keep["Raw email bodies"] == "30 days"
    assert "append-only" in keep["Audit log"] and "agree with client" in keep["Thread checkpoints"]
