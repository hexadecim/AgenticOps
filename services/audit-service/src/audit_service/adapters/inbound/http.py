"""Audit search API (through the gateway). Read-only: there is no route that changes an entry.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

import base64
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal

from ...application import NotAllowedError
from ...domain.entry import AuditEntry, AuditQuery, Kind


class BadCursorError(Exception):
    pass


def encode_cursor(before: tuple[datetime, str]) -> str:
    return base64.urlsafe_b64encode(f"{before[0].isoformat()}|{before[1]}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        at, ident = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(at), ident
    except ValueError as exc:
        raise BadCursorError() from exc


def entry_json(e: AuditEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "time": e.occurred_at.isoformat(),
        "kind": e.kind.value,
        "actor": e.actor,
        "event": e.event,
        "detail": e.detail,
        "ticket": e.ticket,
        "privacy": e.privacy,
        "thread_id": e.thread_id,
        "event_type": e.event_type,
        "source": e.source,
        "correlation_id": e.correlation_id,
    }


def install_error_handlers(app: FastAPI) -> None:
    async def forbidden(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"code": "forbidden", "message": "Your role cannot read the audit log."}, status_code=403
        )

    async def bad_cursor(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"code": "invalid_cursor", "message": "The page link is invalid."}, status_code=422
        )

    app.add_exception_handler(NotAllowedError, forbidden)
    app.add_exception_handler(BadCursorError, bad_cursor)


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["audit"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.get("/entries")
    async def entries(
        principal: SignedIn,
        kind: Kind | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        query = AuditQuery(kind, q, decode_cursor(cursor) if cursor else None, limit)
        page = await container.search(principal, query)
        return {
            "entries": [entry_json(e) for e in page.entries],
            "next_cursor": encode_cursor(page.next_before) if page.next_before else None,
        }

    return router
