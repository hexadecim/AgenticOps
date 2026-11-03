"""Internal API for the orchestrator. Jira writes have no HTTP route: they only follow approval events.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from ...domain.errors import (
    JiraError,
    SettingsUnavailableError,
    TrackerRejectedError,
    TrackerUnavailableError,
)


def install_error_handlers(app: FastAPI) -> None:
    codes: dict[type[JiraError], int] = {
        TrackerUnavailableError: 503,
        SettingsUnavailableError: 503,
        TrackerRejectedError: 502,
    }

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, JiraError)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=codes.get(type(exc), 400))

    app.add_exception_handler(JiraError, handle)


def build_internal_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    Caller = Annotated[str, Depends(container.require_service)]

    @router.get("/context")
    async def context(_: Caller, hours: Annotated[int, Query(ge=1, le=168)] = 24) -> dict[str, Any]:
        ctx = await container.get_context(hours)
        return {
            "configured": ctx.configured,
            "project": ctx.project,
            "issue_type": ctx.issue_type,
            "hours": hours,
            "recent": [
                {"key": i.key, "summary": i.summary, "status": i.status, "created": i.created}
                for i in ctx.recent
            ],
        }

    return router
