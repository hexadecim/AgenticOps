"""Dashboard API (through the gateway). Counts, timings and tokenised titles only.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from opspilot_common.auth import Principal


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["reporting"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.get("/dashboard")
    async def dashboard(_: SignedIn) -> dict[str, Any]:
        d = await container.get_dashboard()
        latest = d.incidents[0].key if d.incidents else None
        timeline = await container.get_timeline(latest) if latest else []
        return {
            "generated_at": d.generated_at.isoformat(),
            "window_hours": 24,
            "dependencies": [
                {
                    "name": x.name,
                    "label": x.label,
                    "health": x.health.value,
                    "since": _iso(x.since),
                    "callers": x.callers,
                }
                for x in d.dependencies
            ],
            "stages": [
                {"id": s.stage.value, "label": s.label, "count": s.count, "note": s.note} for s in d.stages
            ],
            "service_levels": [
                {"id": s.id, "label": s.label, "value": s.value, "detail": s.detail, "breached": s.breached}
                for s in d.service_levels
            ],
            "incidents": [
                {
                    "key": i.key,
                    "title": i.title,
                    "severity": i.severity,
                    "site": i.site,
                    "reports": i.reports,
                    "first_seen": _iso(i.first_seen),
                    "status": i.status,
                }
                for i in d.incidents
            ],
            "timeline": {"key": latest, "entries": [_entry(e) for e in timeline]},
        }

    @router.get("/incidents/{key}/timeline")
    async def incident_timeline(key: str, _: SignedIn) -> dict[str, Any]:
        return {"key": key, "entries": [_entry(e) for e in await container.get_timeline(key)]}

    return router


def _entry(e: Any) -> dict[str, Any]:
    return {"at": e.at.isoformat(), "actor": e.actor, "text": e.text, "system": e.system}
