"""ops-service HTTP API. Internal (orchestrator): diagnostics and probes. People (through the gateway): fresh
evidence for a pending fix. There is no route that runs an action: actions only follow approval events.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal
from pydantic import BaseModel, Field

from ...application import DiagnoseQuery
from ...domain.errors import (
    EvidenceUnavailableError,
    NotAllowedError,
    NotFoundError,
    OpsError,
    SettingsUnavailableError,
)


class DiagnoseBody(BaseModel):
    text: str = Field(max_length=4000, description="Tokenised problem description")
    site: str | None = Field(default=None, max_length=40)
    asset: str | None = Field(default=None, max_length=60)
    run_id: str | None = Field(default=None, max_length=36)
    thread_id: str | None = Field(default=None, max_length=36)


def install_error_handlers(app: FastAPI) -> None:
    codes: dict[type[OpsError], int] = {
        NotFoundError: 404,
        NotAllowedError: 403,
        EvidenceUnavailableError: 503,
        SettingsUnavailableError: 503,
    }

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, OpsError)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=codes.get(type(exc), 400))

    app.add_exception_handler(OpsError, handle)


def build_internal_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    Caller = Annotated[str, Depends(container.require_service)]

    @router.post("/diagnostics")
    async def diagnostics(body: DiagnoseBody, _: Caller) -> dict[str, Any]:
        result = await container.diagnose(DiagnoseQuery(**body.model_dump()))
        return dict(result.json())

    @router.get("/probes")
    async def probes(
        _: Caller,
        region: Annotated[str | None, Query(max_length=40)] = None,
        run_id: Annotated[str | None, Query(max_length=36)] = None,
        thread_id: Annotated[str | None, Query(max_length=36)] = None,
    ) -> dict[str, Any]:
        results = await container.check_health(region, run_id=run_id, thread_id=thread_id)
        return {"checked_at": container.clock.now().isoformat(), "probes": [p.json() for p in results]}

    return router


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["ops"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.get("/fixes/{approval_id}/evidence")
    async def evidence(approval_id: str, principal: SignedIn) -> dict[str, Any]:
        return dict(await container.get_evidence(principal, approval_id))

    return router
