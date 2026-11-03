"""HTTP API. `/v1/*` for signed-in people (through the gateway), `/internal/v1/*` for services only.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal

from ...domain.errors import EmailNotFoundError, MailError
from ...domain.mail import Address, StoredEmail
from ...domain.status import MailboxStatus


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def status_json(status: MailboxStatus) -> dict[str, Any]:
    return {
        "level": status.level.value,
        "message": status.message,
        "configured": status.configured,
        "provider": status.provider,
        "folder": status.folder,
        "poll_interval_seconds": status.poll_interval_seconds,
        "last_poll_at": _iso(status.last_poll_at),
        "last_success_at": _iso(status.last_success_at),
        "last_error": status.last_error,
        "circuit": status.circuit,
        "total_received": status.total_received,
    }


def _addr(a: Address) -> dict[str, str]:
    return {"email": a.email, "name": a.name}


def email_json(email: StoredEmail) -> dict[str, Any]:
    m = email.mail
    return {
        "id": email.id,
        "thread_id": email.thread_id,
        "provider": email.provider,
        "fetched_at": _iso(email.fetched_at),
        "received_at": _iso(m.received_at),
        "message_id": m.message_id,
        "in_reply_to": m.in_reply_to,
        "references": list(m.references),
        "subject": m.subject,
        "from": _addr(m.sender),
        "to": [_addr(a) for a in m.to],
        "cc": [_addr(a) for a in m.cc],
        "body_text": m.body_text,
        "body_html": m.body_html,
        "attachments": [asdict(a) for a in m.attachments],
    }


def install_error_handlers(app: FastAPI) -> None:
    codes: dict[type[MailError], int] = {EmailNotFoundError: 404}

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, MailError)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=codes.get(type(exc), 400))

    app.add_exception_handler(MailError, handle)


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["mail"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]
    AdminUser = Annotated[Principal, Depends(container.require_admin)]

    @router.get("/status")
    async def status(_: SignedIn) -> dict[str, Any]:
        return status_json(await container.get_status())

    @router.post("/poll")
    async def poll_now(_: AdminUser) -> dict[str, Any]:
        result = await container.poll()
        return {
            "outcome": result.outcome.value,
            "new": result.new,
            "detail": result.detail,
            "status": status_json(await container.get_status()),
        }

    return router


def build_internal_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    Caller = Annotated[str, Depends(container.require_reader_service)]

    @router.get("/emails/{email_id}")
    async def get_email(email_id: str, _: Caller, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return email_json(await container.get_email(email_id))

    return router
