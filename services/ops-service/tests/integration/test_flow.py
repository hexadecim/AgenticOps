"""ops-service end to end on SQLite with the real mock environment, runbooks and log files from deploy/ops:
diagnose → T2 proposal → approval → job → mock executor → action.executed → probes healthy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from ops_service.adapters.inbound.events import on_event
from ops_service.adapters.outbound.mock_world import load_spec
from ops_service.application import DiagnoseQuery
from ops_service.bootstrap import Container, build_container
from ops_service.config import Settings
from ops_service.domain.errors import EmbeddingUnavailableError, SettingsUnavailableError
from ops_service.domain.model import OpsSettings
from ops_service.main import create_app
from opspilot_common.auth import Principal, Role, ServiceTokenIssuer, TokenIssuer
from opspilot_common.events import EventEnvelope, Inbox
from opspilot_common.events.catalogue import ApprovalDecided, ApprovalRequested, Decision, Gate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

OPS = Path(__file__).parents[4] / "deploy" / "ops"
NOW = datetime(2026, 11, 3, 10, 31, tzinfo=UTC)
SECRET = "s" * 32
PROBLEM = "upload failure: Upload stuck at 0% on M-2210, telemetry file never arrives"


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeSettings:
    def __init__(self) -> None:
        self.current = OpsSettings(runbook_folder=str(OPS / "runbooks"), log_folder=str(OPS / "logs"))
        self.fail = False

    async def ops(self) -> OpsSettings:
        if self.fail:
            raise SettingsUnavailableError("down")
        return self.current


class FakeEmbedder:
    """Bag-of-words vectors over a tiny vocabulary: enough to rank the upload runbook first."""

    VOCAB = ("upload", "storage", "eio", "login", "sign", "report", "analytics", "worker", "received")

    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    async def embed(self, settings: OpsSettings, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingUnavailableError("ollama down")
        return [[float(t.lower().count(w)) + 0.01 for w in self.VOCAB] for t in texts]


class World:
    def __init__(self, c: Container, settings: FakeSettings, embedder: FakeEmbedder) -> None:
        self.c, self.settings, self.embedder = c, settings, embedder
        self.handler = Inbox(c.tables.inbox, "ops-service.events").guard(
            c.sessions, on_event(c.store_proposal, c.queue_fix, c.active, lambda: None, lambda: None)
        )

    async def events(self, kind: str) -> list[dict[str, Any]]:
        async with self.c.sessions() as s:
            rows = [json.loads(b) for b in (await s.execute(select(self.c.tables.outbox.c.body))).scalars()]
        return [r["payload"] for r in rows if r["type"] == kind]

    async def propose(self, diagnostics: dict[str, Any], candidate: int = 0) -> str:
        fix = diagnostics["candidates"][candidate]
        await self.handler(
            EventEnvelope.wrap(
                ApprovalRequested(
                    approval_id="ap-fix-1", run_id="run-1", thread_id="t-1", gate=Gate.FIX,
                    title="Restart upload-worker on eu-west-1?", detail="Diagnostics", jira_key="KAN-12",
                    proposal={
                        "action": {"name": fix["action"], "params": fix["params"],
                                   "params_spec": fix["params_spec"]},
                        "region": diagnostics["region"], "site": diagnostics["site"],
                        "services": ["upload-worker"],
                    },
                ),
                source="agent-orchestrator",
            )
        )  # fmt: skip
        return "ap-fix-1"

    async def decide(
        self, decision: Decision = Decision.APPROVE, edits: dict[str, Any] | None = None
    ) -> None:
        await self.handler(
            EventEnvelope.wrap(
                ApprovalDecided(
                    approval_id="ap-fix-1", run_id="run-1", thread_id="t-1", gate=Gate.FIX, decision=decision,
                    decided_by="u-2", decided_by_role="support_agent", edits=edits or {},
                    reason=None if decision is Decision.APPROVE else "Engineering is on it",
                ),
                source="approval-service",
            )
        )  # fmt: skip


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    settings, embedder = FakeSettings(), FakeEmbedder()
    container = build_container(
        Settings(jwt_secret=SECRET, log_json=False),
        engine=engine,
        schema="",
        redis=fakeredis.aioredis.FakeRedis(),
        source=settings,
        embedder=embedder,
        spec=load_spec(OPS / "environment.yaml"),
        clock=Clock(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(container.tables.metadata.create_all)
    assert await container.world.seed() == 1
    assert await container.world.seed() == 0  # only the very first start opens faults
    yield World(container, settings, embedder)
    await engine.dispose()


async def test_diagnose_finds_failing_probe_masked_logs_runbook_and_allow_listed_fix(world: World) -> None:
    result = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014", run_id="run-1"))).json()
    assert result["region"] == "eu-west-1"
    probes = {p["name"]: p for p in result["probes"]}
    assert not probes["Upload"]["ok"] and probes["Upload"]["detail"] == "timeout after 30 s, 0 bytes stored"
    assert all(p["ok"] for n, p in probes.items() if n != "Upload")
    worker, api = result["logs"]  # both serve the Upload probe; the noisier one (the suspect) comes first
    assert worker["service"] == "upload-worker" and worker["since"] == "09:58:40" and worker["similar"] == 4
    assert api["service"] == "upload-api" and "Bearer ***" in api["lines"][0]
    text = "\n".join(worker["lines"] + api["lines"])
    assert "@" not in text and "+49" not in text and "10.24" not in text and "user=***" in text
    assert result["runbooks"][0]["id"] == "RB-07" and result["runbooks"][0]["method"] == "semantic"
    first = result["candidates"][0]
    assert first["signature"] == 'restart_service(service="upload-worker", region="eu-west-1")'
    assert first["runbook"] == {
        "id": "RB-07", "title": "Upload storage errors", "step": 3,
        "why": "the worker holds a stale storage handle after a volume failover",
    }  # fmt: skip
    assert result["impact"] == {"sites": ["SITE-A014", "SITE-B207", "SITE-C031"], "uploads waiting": 41}
    assert result["deployment"].startswith("Last deployment: upload-api v2.14.1")
    # Probe changes are events (first sighting of each probe); a check with no change publishes nothing.
    assert len(await world.events("probe.status")) == 5
    await world.c.check_health("eu-west-1")
    assert len(await world.events("probe.status")) == 5
    await world.c.check_health("eu-west-1", announce=True)  # start-up sweep: current state of every probe
    assert len(await world.events("probe.status")) == 10


async def test_unreadable_log_folder_is_a_note_not_silence(world: World, tmp_path: Path) -> None:
    # Seen live: an emptied bind mount made every log search return nothing, with no sign of it.
    world.settings.current = OpsSettings(runbook_folder=str(OPS / "runbooks"), log_folder=str(tmp_path))
    result = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    assert result["logs"] == []
    assert any(n.startswith("Logs unavailable") for n in result["notes"])
    # The service still follows the runbook (upload-worker), not the probe's first service.
    assert result["candidates"][0]["params"]["service"] == "upload-worker"


async def test_runbook_search_falls_back_to_keywords_when_embeddings_are_down(world: World) -> None:
    world.embedder.fail = True
    result = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    assert result["runbooks"][0]["id"] == "RB-07" and result["runbooks"][0]["method"] == "keyword"
    assert any("keywords" in n for n in result["notes"])
    assert result["candidates"][0]["action"] == "restart_service"


async def test_approved_fix_runs_once_and_heals_the_probe(world: World) -> None:
    diagnostics = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    await world.propose(diagnostics)
    await world.decide()
    await world.decide()  # redelivered: one job only
    assert await world.c.run_fixes() == 1
    assert await world.c.run_fixes() == 0
    (executed,) = await world.events("action.executed")
    assert executed["ok"] and executed["executor"] == "mock" and executed["jira_key"] == "KAN-12"
    assert executed["params"] == {"service": "upload-worker", "region": "eu-west-1"}
    assert (
        "no real system was touched" in executed["detail"]
        and "(before: Upload failing)" in executed["detail"]
    )
    probes = await world.c.check_health("eu-west-1")
    assert all(p.ok for p in probes)
    env = await world.c.world.environment()
    assert env.impact == {"uploads waiting": 0}


async def test_rejected_or_off_list_edits_never_run(world: World) -> None:
    diagnostics = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    await world.propose(diagnostics)
    await world.decide(Decision.REJECT)
    assert await world.c.run_fixes() == 0 and await world.events("action.executed") == []
    # Defence in depth: even an approval (forged or buggy) with a parameter outside the offered choices is
    # refused before anything runs.
    await world.decide(Decision.APPROVE, {"params": {"service": "postgres"}})
    (refused,) = await world.events("action.executed")
    assert not refused["ok"] and "Service 'postgres' is not allowed here" in refused["detail"]
    assert await world.c.run_fixes() == 0
    assert not (await world.c.check_health("eu-west-1"))[2].ok  # Upload still failing: nothing ran


async def test_action_disabled_after_approval_does_not_run(world: World) -> None:
    diagnostics = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    await world.propose(diagnostics)
    await world.decide()
    world.settings.current = OpsSettings(
        runbook_folder=str(OPS / "runbooks"), log_folder=str(OPS / "logs"), enabled_actions=frozenset()
    )
    await world.c.active.get(refresh=True)
    await world.c.run_fixes()
    (executed,) = await world.events("action.executed")
    assert not executed["ok"] and "disabled after approval" in executed["detail"]


async def test_http_evidence_for_people_and_internal_routes_for_the_orchestrator(world: World) -> None:
    diagnostics = (await world.c.diagnose(DiagnoseQuery(PROBLEM, site="SITE-A014"))).json()
    await world.propose(diagnostics)
    app = create_app(world.c, with_events=False, with_worker=False)
    people = TokenIssuer(SECRET)
    agent = people.issue(Principal("u-2", "Agent", frozenset({Role.SUPPORT_AGENT})))
    admin = people.issue(Principal("u-9", "Admin", frozenset({Role.ADMIN})))
    service = ServiceTokenIssuer(SECRET, "agent-orchestrator").header()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ops") as http:
        r = await http.get("/v1/fixes/ap-fix-1/evidence", headers={"Authorization": f"Bearer {agent}"})
        assert r.status_code == 200
        body = r.json()
        assert body["executor"] == "mock" and body["enabled"] and body["job"] is None
        assert [p["name"] for p in body["probes"] if not p["ok"]] == ["Upload"]
        assert body["logs"][0]["service"] == "upload-worker"
        r = await http.get("/v1/fixes/ap-fix-1/evidence", headers={"Authorization": f"Bearer {admin}"})
        assert r.status_code == 403
        r = await http.get("/v1/fixes/nope/evidence", headers={"Authorization": f"Bearer {agent}"})
        assert r.status_code == 404
        r = await http.post(
            "/internal/v1/diagnostics", json={"text": PROBLEM, "site": "SITE-A014"}, headers=service
        )
        assert r.status_code == 200 and r.json()["candidates"][0]["action"] == "restart_service"
        r = await http.get("/internal/v1/probes", params={"region": "eu-west-1"}, headers=service)
        assert r.status_code == 200 and len(r.json()["probes"]) == 5
        other = ServiceTokenIssuer(SECRET, "mail-service").header()
        assert (
            await http.post("/internal/v1/diagnostics", json={"text": "x"}, headers=other)
        ).status_code == 403
        r = await http.post(
            "/internal/v1/diagnostics", json={"text": "x"}, headers={"Authorization": f"Bearer {agent}"}
        )
        assert r.status_code == 403
