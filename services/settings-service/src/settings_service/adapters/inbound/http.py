"""HTTP API (Admin role only). Secret values are write-only: responses say whether each secret is set.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal
from pydantic import BaseModel, Field

from ...application import SectionView
from ...domain.errors import (
    InvalidSettingsError,
    ReaderNotAllowedError,
    SettingsError,
    UnknownSectionError,
    VersionConflictError,
)
from ...domain.spec import SectionSpec


class SaveBody(BaseModel):
    version: int = Field(ge=0, description="The version you loaded; saving fails if someone saved since")
    values: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(
        default_factory=dict, description="Omitted or null = keep, empty string = clear, text = replace"
    )


class TestBody(BaseModel):
    values: dict[str, Any] | None = None
    secrets: dict[str, str | None] = Field(default_factory=dict)


def spec_json(spec: SectionSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "title": spec.title,
        "description": spec.description,
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "kind": f.kind,
                "required": f.required,
                "default": f.default,
                "help": f.help,
                "placeholder": f.placeholder,
                "minimum": f.minimum,
                "maximum": f.maximum,
                "options": [{"value": o.value, "label": o.label} for o in f.options],
                "when": dict(f.when),
            }
            for f in spec.fields
        ],
    }


def view_json(view: SectionView) -> dict[str, Any]:
    return {
        "section": spec_json(view.spec),
        "version": view.version,
        "values": view.values,
        "secrets_set": view.secrets_set,
        "updated_at": view.updated_at.isoformat() if view.updated_at else None,
        "updated_by": view.updated_by,
    }


def install_error_handlers(app: FastAPI) -> None:
    codes = {
        UnknownSectionError: 404,
        VersionConflictError: 409,
        InvalidSettingsError: 422,
        ReaderNotAllowedError: 403,
    }

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, SettingsError)
        body: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, InvalidSettingsError):
            body["errors"] = exc.errors
        return JSONResponse(body, status_code=codes.get(type(exc), 400))

    app.add_exception_handler(SettingsError, handle)


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["settings"])
    AdminUser = Annotated[Principal, Depends(container.require_admin)]

    @router.get("/sections")
    async def list_sections(_: AdminUser) -> list[dict[str, Any]]:
        return [view_json(v) for v in await container.get_section.all()]

    @router.get("/sections/{section}")
    async def get_section(section: str, _: AdminUser) -> dict[str, Any]:
        return view_json(await container.get_section(section))

    @router.put("/sections/{section}")
    async def save_section(section: str, body: SaveBody, actor: AdminUser) -> dict[str, Any]:
        return view_json(
            await container.save_section(actor.user_id, section, body.values, body.secrets, body.version)
        )

    @router.post("/sections/{section}/test", status_code=status.HTTP_200_OK)
    async def test_section(section: str, body: TestBody, _: AdminUser) -> dict[str, Any]:
        report = await container.run_connection_test(section, body.values, body.secrets)
        return {
            "ok": report.ok,
            "summary": report.summary,
            "duration_ms": report.duration_ms,
            "checks": [asdict(c) for c in report.checks],
        }

    return router


def build_internal_router(container: Any) -> APIRouter:
    """Service-to-service API (service token required). Not routed by the gateway."""
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    Caller = Annotated[str, Depends(container.require_service)]

    @router.get("/sections/{section}/resolved")
    async def resolved(section: str, caller: Caller, response: Response) -> dict[str, Any]:
        view = await container.resolve_section(caller, section)
        response.headers["Cache-Control"] = "no-store"
        return {
            "section": view.section,
            "version": view.version,
            "configured": view.configured,
            "values": view.values,
            "secrets": view.secrets,
        }

    return router
