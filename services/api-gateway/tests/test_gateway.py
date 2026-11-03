from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis.aioredis
import httpx
import pytest
import respx
from api_gateway.adapters.outbound.rate_limit import RedisRateLimiter
from api_gateway.bootstrap import build_container
from api_gateway.config import Settings
from api_gateway.main import create_app
from opspilot_common.auth import Principal, Role, TokenIssuer
from opspilot_common.resilience import ResilienceRegistry

IDENTITY = "http://identity.test"
SETTINGS = "http://settings.test"
MAIL = "http://mail.test"
PRIVACY = "http://privacy.test"
AUDIT = "http://audit.test"
APPROVALS = "http://approval.test"
RUNS = "http://orchestrator.test"
REPORTING = "http://reporting.test"
OPS = "http://ops.test"
JWT = "x" * 32
WEB = {"X-OpsPilot-Client": "web"}
USER = {"id": "u1", "email": "lena@example.eu", "name": "Lena", "roles": ["l1_analyst"]}


REDIS = fakeredis.aioredis.FakeRedis()


def session(*roles: Role) -> dict[str, str]:
    token = TokenIssuer(JWT).issue(Principal("u1", "Lena", frozenset(roles)))
    return {"Cookie": f"opspilot_session={token}"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    await REDIS.flushall()
    settings = Settings(
        identity_url=IDENTITY,
        settings_url=SETTINGS,
        mail_url=MAIL,
        privacy_url=PRIVACY,
        audit_url=AUDIT,
        approval_url=APPROVALS,
        orchestrator_url=RUNS,
        reporting_url=REPORTING,
        ops_url=OPS,
        live_events=False,
        jwt_secret=JWT,
        sign_in_limit_per_minute=3,
        log_json=False,
    )
    redis = REDIS
    limiter = RedisRateLimiter(redis, ResilienceRegistry().get("redis"))
    container = build_container(settings, limiter=limiter, redis=redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://gateway"
    ) as c:
        yield c


@respx.mock
async def test_mfa_step_sets_httponly_cookie_and_hides_token(client: httpx.AsyncClient) -> None:
    respx.post(f"{IDENTITY}/v1/sign-in/mfa").mock(
        return_value=httpx.Response(200, json={"token": "jwt-abc", "expires_in": 1800, "user": USER})
    )
    r = await client.post(
        "/api/auth/sign-in/mfa", headers=WEB, json={"challenge_token": "c", "code": "123456"}
    )
    assert r.status_code == 200
    assert "token" not in r.json()
    cookie = r.headers["set-cookie"]
    assert "opspilot_session=jwt-abc" in cookie
    for attr in ("HttpOnly", "SameSite=strict", "Path=/api", "Max-Age=1800"):
        assert attr.lower() in cookie.lower()


@respx.mock
async def test_cookie_is_forwarded_as_bearer(client: httpx.AsyncClient) -> None:
    route = respx.get(f"{IDENTITY}/v1/me").mock(return_value=httpx.Response(200, json=USER))
    assert (await client.get("/api/auth/me", headers={"Cookie": "opspilot_session=jwt-abc"})).json() == USER
    assert route.calls.last.request.headers["Authorization"] == "Bearer jwt-abc"


async def test_unsafe_requests_need_csrf_header(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/sign-in", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 403 and r.json()["code"] == "csrf"


async def test_no_cookie_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/users")
    assert r.status_code == 401 and r.json()["code"] == "not_signed_in"


@respx.mock
async def test_identity_errors_pass_through(client: httpx.AsyncClient) -> None:
    body = {"code": "invalid_credentials", "message": "Email or password is incorrect."}
    respx.post(f"{IDENTITY}/v1/sign-in").mock(return_value=httpx.Response(401, json=body))
    r = await client.post("/api/auth/sign-in", headers=WEB, json={"email": "a@b.c", "password": "x"})
    assert (r.status_code, r.json()) == (401, body)


@respx.mock
async def test_sign_in_is_rate_limited(client: httpx.AsyncClient) -> None:
    respx.post(f"{IDENTITY}/v1/sign-in").mock(
        return_value=httpx.Response(401, json={"code": "x", "message": "x"})
    )
    codes = [
        (
            await client.post("/api/auth/sign-in", headers=WEB, json={"email": "a@b.c", "password": "x"})
        ).status_code
        for _ in range(4)
    ]
    assert codes == [401, 401, 401, 429]


@respx.mock
async def test_identity_outage_opens_circuit_and_returns_503(client: httpx.AsyncClient) -> None:
    route = respx.get(f"{IDENTITY}/v1/me").mock(side_effect=httpx.ConnectError("refused"))
    session = {"Cookie": "opspilot_session=jwt"}
    statuses = [(await client.get("/api/auth/me", headers=session)).status_code for _ in range(7)]
    assert statuses == [503] * 7
    calls_when_open = route.call_count
    r = await client.get("/api/auth/me", headers=session)
    assert r.json()["code"] == "service_unavailable" and r.json()["dependency"] == "identity-service"
    assert int(r.headers["Retry-After"]) > 0
    assert route.call_count == calls_when_open  # short-circuited, identity not contacted
    health = await client.get("/api/health/ready")
    assert health.status_code == 503 and health.json()["circuits"]["identity-service"] == "open"


@respx.mock
async def test_sign_out_always_clears_cookie(client: httpx.AsyncClient) -> None:
    respx.post(f"{IDENTITY}/v1/sign-out").mock(side_effect=httpx.ConnectError("down"))
    r = await client.post("/api/auth/sign-out", headers={**WEB, "Cookie": "opspilot_session=jwt"})
    assert r.status_code == 204
    assert 'opspilot_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]


@respx.mock
async def test_settings_routes_forward_token_and_body(client: httpx.AsyncClient) -> None:
    route = respx.put(f"{SETTINGS}/v1/sections/jira").mock(
        return_value=httpx.Response(
            422,
            json={
                "code": "invalid_settings",
                "message": "Some fields need attention.",
                "errors": {"email": "x"},
            },
        )
    )
    r = await client.put(
        "/api/admin/settings/jira",
        headers={**WEB, **session(Role.ADMIN)},
        json={"version": 0, "values": {"email": "bad"}},
    )
    assert r.status_code == 422 and r.json()["errors"] == {"email": "x"}
    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ")
    assert b'"email":"bad"' in route.calls.last.request.content.replace(b" ", b"")


async def test_circuits_need_admin_and_report_shared_state(client: httpx.AsyncClient) -> None:
    assert (
        await client.get("/api/admin/system/circuits", headers=session(Role.L1_ANALYST))
    ).status_code == 403
    import json as _json
    import time as _time

    await REDIS.set(
        "opspilot:breaker:jira", _json.dumps({"state": "open", "failures": 5, "opened_at": _time.time()})
    )
    rows = (await client.get("/api/admin/system/circuits", headers=session(Role.ADMIN))).json()
    jira = next(r for r in rows if r["dependency"] == "jira")
    assert jira["state"] == "open" and jira["retry_after_seconds"] > 0 and jira["fail_max"] == 5


@respx.mock
async def test_mail_status_forwards_session_and_survives_mail_service_outage(
    client: httpx.AsyncClient,
) -> None:
    status = {"level": "ok", "message": "Mailbox polled just now", "configured": True}
    route = respx.get(f"{MAIL}/v1/status").mock(return_value=httpx.Response(200, json=status))
    r = await client.get("/api/mail/status", headers=session(Role.L1_ANALYST))
    assert r.status_code == 200 and r.json() == status
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")

    respx.get(f"{MAIL}/v1/status").mock(side_effect=httpx.ConnectError("refused"))
    r = await client.get("/api/mail/status", headers=session(Role.L1_ANALYST))
    assert r.status_code == 503 and r.json()["dependency"] == "mail-service"

    poll = respx.post(f"{MAIL}/v1/poll").mock(
        return_value=httpx.Response(403, json={"code": "forbidden", "message": "no"})
    )
    r = await client.post("/api/admin/mail/poll", headers={**session(Role.L1_ANALYST), **WEB})
    assert r.status_code == 403 and poll.called


@respx.mock
async def test_audit_search_and_identity_reveal_are_forwarded(client: httpx.AsyncClient) -> None:
    audit = respx.get(f"{AUDIT}/v1/entries").mock(
        return_value=httpx.Response(200, json={"entries": [], "next_cursor": None})
    )
    r = await client.get(
        "/api/audit/entries",
        params={"kind": "privacy", "q": "[PERSON_1]"},
        headers=session(Role.COMPLIANCE_LEAD),
    )
    assert r.status_code == 200
    sent = audit.calls.last.request
    assert (
        sent.url.params["kind"] == "privacy"
        and sent.url.params["q"] == "[PERSON_1]"
        and sent.url.params["limit"] == "50"
    )

    reveal = respx.post(f"{PRIVACY}/v1/reveals").mock(
        return_value=httpx.Response(
            200, json={"token": "PERSON_1", "values": {}, "expires_at": "x", "reason": "r"}
        )
    )
    body = {"token": "[PERSON_1]", "reason": "Verify the customer's account"}
    assert (
        await client.post("/api/privacy/reveals", json=body, headers=session(Role.L1_ANALYST))
    ).status_code == 403  # CSRF
    r = await client.post("/api/privacy/reveals", json=body, headers={**session(Role.L1_ANALYST), **WEB})
    assert r.status_code == 200 and reveal.call_count == 1


@respx.mock
async def test_approval_routes_are_forwarded(client: httpx.AsyncClient) -> None:
    queue = respx.get(f"{APPROVALS}/v1/approvals").mock(
        return_value=httpx.Response(200, json={"items": [], "stats": {}})
    )
    r = await client.get("/api/approvals", params={"gate": "ticket"}, headers=session(Role.L1_ANALYST))
    assert r.status_code == 200 and queue.calls.last.request.url.params["gate"] == "ticket"
    decide = respx.post(f"{APPROVALS}/v1/approvals/a-1/decision").mock(
        return_value=httpx.Response(
            409, json={"code": "already_decided", "message": "Someone already decided this"}
        )
    )
    r = await client.post(
        "/api/approvals/a-1/decision",
        json={"decision": "approve"},
        headers={**session(Role.L1_ANALYST), **WEB},
    )
    assert r.status_code == 409 and r.json()["code"] == "already_decided" and decide.called
    respx.get(f"{RUNS}/v1/runs/summary").mock(return_value=httpx.Response(200, json={"ai_paused": True}))
    assert (await client.get("/api/runs/summary", headers=session(Role.L1_ANALYST))).json() == {
        "ai_paused": True
    }


async def test_live_events_relay_ids_only() -> None:
    import asyncio

    from api_gateway.adapters.inbound.live import Broadcaster
    from opspilot_common.events import EventEnvelope
    from opspilot_common.events.catalogue import ApprovalRequested, Gate

    live = Broadcaster()
    stream = live.stream(heartbeat=0.05)
    assert await anext(stream) == "retry: 5000\n\n"
    assert await anext(stream) == ": ping\n\n"
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await live.relay(
        EventEnvelope.wrap(
            ApprovalRequested(
                approval_id="a-1",
                run_id="r-1",
                thread_id="t-1",
                gate=Gate.TICKET,
                title="Upload stuck",
                detail="secret detail",
                proposal={"email": {"body": "Hello [PERSON_1]"}},
            ),
            source="agent-orchestrator",
        )
    )
    message = await pending
    assert message.startswith("event: approval.requested\n") and '"approval_id": "a-1"' in message
    assert "Upload stuck" not in message and "PERSON_1" not in message and "secret" not in message
    await stream.aclose()
    assert live.subscribers == 0


@respx.mock
async def test_dashboard_routes_are_forwarded(client: httpx.AsyncClient) -> None:
    respx.get(f"{REPORTING}/v1/dashboard").mock(return_value=httpx.Response(200, json={"stages": []}))
    respx.get(f"{REPORTING}/v1/incidents/KAN-19/timeline").mock(
        return_value=httpx.Response(200, json={"entries": []})
    )
    assert (await client.get("/api/dashboard", headers=session(Role.SUPPORT_MANAGER))).json() == {
        "stages": []
    }
    r = await client.get("/api/incidents/KAN-19/timeline", headers=session(Role.SUPPORT_MANAGER))
    assert r.json() == {"entries": []}


@respx.mock
async def test_fix_evidence_is_fresh_and_503_when_ops_service_is_down(client: httpx.AsyncClient) -> None:
    evidence = {"checked_at": "2026-11-03T10:31:00+00:00", "probes": [{"name": "Upload", "ok": False}]}
    route = respx.get(f"{OPS}/v1/fixes/a-1/evidence").mock(return_value=httpx.Response(200, json=evidence))
    r = await client.get("/api/fixes/a-1/evidence", headers=session(Role.SUPPORT_AGENT))
    assert r.status_code == 200 and r.json() == evidence
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")
    respx.get(f"{OPS}/v1/fixes/a-1/evidence").mock(side_effect=httpx.ConnectError("refused"))
    for _ in range(6):  # breaker opens after repeated failures; the answer stays a clean 503
        r = await client.get("/api/fixes/a-1/evidence", headers=session(Role.SUPPORT_AGENT))
        assert r.status_code == 503 and r.json()["dependency"] == "ops-service"
