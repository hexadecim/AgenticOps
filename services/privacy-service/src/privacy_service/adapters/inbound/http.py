"""HTTP API. `/v1/reveals` for people (through the gateway); `/internal/v1/*` for services only.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal
from pydantic import BaseModel, Field

from ...domain.errors import (
    EmailNotFoundError,
    InvalidRequestError,
    LeakDetectedError,
    NotAllowedError,
    PrivacyError,
    PrivacyUnavailableError,
    UnknownTokenError,
)
from ...domain.records import PseudonymisationRecord, Reveal
from ..outbound.sql import email_to_json


class RevealBody(BaseModel):
    token: str = Field(max_length=40, examples=["[PERSON_1]"])
    reason: str = Field(max_length=300, examples=["Verify the customer's account"])
    thread_id: str | None = Field(default=None, max_length=36)


class DetokeniseBody(BaseModel):
    text: str = Field(max_length=100_000)
    purpose: str = Field(max_length=100, examples=["customer reply"])
    thread_id: str | None = Field(default=None, max_length=36)


def record_json(record: PseudonymisationRecord) -> dict[str, Any]:
    return {
        "record_id": record.id,
        "summary": record.summary(),
        "counts": record.counts,
        "signature_removed": record.signature_removed,
        "quoted_removed": record.quoted_removed,
        "created_at": record.created_at.isoformat(),
        "email": email_to_json(record.email),
    }


def reveal_json(reveal: Reveal) -> dict[str, Any]:
    return {
        "token": reveal.token,
        "values": reveal.values,
        "expires_at": reveal.expires_at.isoformat(),
        "reason": reveal.reason,
    }


def install_error_handlers(app: FastAPI) -> None:
    codes: dict[type[PrivacyError], int] = {
        PrivacyUnavailableError: 503,
        LeakDetectedError: 422,  # a deliberate refusal, not an outage: callers must not retry it
        EmailNotFoundError: 404,
        UnknownTokenError: 404,
        NotAllowedError: 403,
        InvalidRequestError: 422,
    }

    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, PrivacyError)
        status = codes.get(type(exc), 400)
        headers = {"Retry-After": "30"} if status == 503 else None
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status, headers=headers)

    app.add_exception_handler(PrivacyError, handle)


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["privacy"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.post("/reveals")
    async def reveal(body: RevealBody, principal: SignedIn, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return reveal_json(await container.reveal(principal, body.token, body.reason, body.thread_id))

    return router


def build_internal_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    Caller = Annotated[str, Depends(container.require_service)]

    @router.post("/emails/{email_id}/pseudonymise")
    async def pseudonymise(email_id: str, caller: Caller) -> dict[str, Any]:
        return record_json(await container.pseudonymise(email_id, caller))

    @router.post("/detokenise")
    async def detokenise(body: DetokeniseBody, caller: Caller, response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        text = await container.detokenise(caller, body.text, purpose=body.purpose, thread_id=body.thread_id)
        return {"text": text}

    return router
