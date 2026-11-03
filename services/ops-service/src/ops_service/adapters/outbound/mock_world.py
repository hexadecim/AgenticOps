"""The mock production environment (default): simulated regions, sites, services, probes and an executor that
changes only this simulation. Nothing here reaches a real system.

The environment is described in a YAML file (deploy/ops/environment.yaml); which services are faulty is kept
in the `ops` schema so a fix survives restarts. Re-open a fault for a demo with
`python -m ops_service.mock_fault upload-worker eu-west-1`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.model import Environment, ProbeResult
from ...ports import Clock, ExecutionResult
from .sql import OpsTables


def load_spec(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    return raw


class MockWorld:
    """EnvironmentSource + ProbeRunner + ActionExecutor over one simulated environment."""

    name = "mock"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tables: OpsTables,
        spec: dict[str, Any],
        clock: Clock,
    ) -> None:
        self._sessions, self._t, self._spec, self._clock = sessions, tables, spec, clock
        self._probes: list[dict[str, Any]] = list(spec.get("probes", []))

    # --- set-up ---------------------------------------------------------------------------------------
    async def seed(self) -> int:
        """Open the YAML's initial faults the first time the service starts (never again after that)."""
        async with self._sessions() as s, s.begin():
            if (await s.execute(select(func.count()).select_from(self._t.mock_faults))).scalar_one():
                return 0
            now = self._clock.now()
            faults = list(self._spec.get("faults", []))
            for f in faults:
                await s.execute(
                    insert(self._t.mock_faults).values(
                        service=f["service"], region=f["region"], detail=f.get("detail", "fault"), since=now
                    )
                )
        return len(faults)

    async def inject(self, service: str, region: str, detail: str) -> None:
        """(Re)open a fault, for demos and tests."""
        now = self._clock.now()
        key = (self._t.mock_faults.c.service == service, self._t.mock_faults.c.region == region)
        async with self._sessions() as s, s.begin():
            result = await s.execute(
                update(self._t.mock_faults).where(*key).values(detail=detail, since=now, cleared_at=None)
            )
            if not result.rowcount:  # type: ignore[attr-defined]
                await s.execute(
                    insert(self._t.mock_faults).values(
                        service=service, region=region, detail=detail, since=now
                    )
                )

    async def _open_faults(self) -> dict[tuple[str, str], str]:
        async with self._sessions() as s:
            rows = await s.execute(
                select(self._t.mock_faults).where(self._t.mock_faults.c.cleared_at.is_(None))
            )
            return {(r.service, r.region): r.detail for r in rows}

    # --- EnvironmentSource -------------------------------------------------------------------------------
    async def environment(self) -> Environment:
        faults = await self._open_faults()
        failing_probes = {
            p["name"] for p in self._probes for (svc, _), _d in faults.items() if svc in p.get("services", [])
        }
        impact = {
            str(i["label"]): int(i["value"]) if i.get("probe") in failing_probes else 0
            for i in self._spec.get("impact", [])
        }
        return Environment(
            regions=tuple(self._spec.get("regions", [])),
            sites={str(k): str(v) for k, v in (self._spec.get("sites") or {}).items()},
            services=tuple(self._spec.get("services", [])),
            deployment_note=self._spec.get("deployment"),
            impact=impact,
        )

    # --- ProbeRunner ------------------------------------------------------------------------------------
    async def run(self, region: str | None) -> list[ProbeResult]:
        faults = await self._open_faults()
        regions = [region] if region else list(self._spec.get("regions", [])) or [None]
        out = []
        for r in regions:
            for p in self._probes:
                services = tuple(p.get("services", []))
                broken = [faults[(s, r)] for s in services if r and (s, r) in faults]
                out.append(
                    ProbeResult(
                        name=p["name"],
                        ok=not broken,
                        detail=p.get("fail", broken[0]) if broken else p.get("ok", "ok"),
                        latency_ms=None if broken else p.get("latency_ms"),
                        services=services,
                        region=r,
                    )
                )
        return out

    # --- ActionExecutor ---------------------------------------------------------------------------------
    async def execute(self, action: str, params: dict[str, str], *, key: str) -> ExecutionResult:
        async with self._sessions() as s, s.begin():
            done = (await s.execute(select(self._t.mock_runs).where(self._t.mock_runs.c.key == key))).first()
            if done:  # same approval again: report what happened, do not run twice
                return ExecutionResult(bool(done.ok), str(done.detail))
            result = await self._apply(s, action, params, self._clock.now())
            await s.execute(
                insert(self._t.mock_runs).values(
                    key=key, action=action, params=params, ok=result.ok, detail=result.detail,
                    ran_at=self._clock.now(),
                )
            )  # fmt: skip
        return result

    async def _apply(
        self, s: AsyncSession, action: str, params: dict[str, str], now: datetime
    ) -> ExecutionResult:
        if action == "restart_service":
            service, region = params["service"], params["region"]
            t = self._t.mock_faults
            cleared = await s.execute(
                update(t)
                .where(t.c.service == service, t.c.region == region, t.c.cleared_at.is_(None))
                .values(cleared_at=now)
            )
            was = "it held a stale handle; cleared" if cleared.rowcount else "it was not faulty"  # type: ignore[attr-defined]
            return ExecutionResult(
                True, f"Mock executor restarted {service} in {region} ({was}); no real system was touched"
            )
        if action == "retry_failed_uploads":
            site = params["site"]
            return ExecutionResult(
                True, f"Mock executor queued the failed uploads of {site} again; no real system was touched"
            )
        return ExecutionResult(False, f"The mock executor cannot run {action}")
