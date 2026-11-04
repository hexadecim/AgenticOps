"""HTTP API. `/v1/reveals` for people (through the gateway); `/internal/v1/*` for services only.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal
from opspilot_common.subjects import StoreReport
from pydantic import BaseModel, Field

from ...domain.dsr import LABELS, DataRequest, due_label
from ...domain.errors import (
    EmailNotFoundError,
    InvalidRequestError,
    LeakDetectedError,
    NotAllowedError,
    PrivacyError,
    PrivacyUnavailableError,
    RequestNotFoundError,
    StoresUnavailableError,
    SubjectBusyError,
    UnknownTokenError,
)
from ...domain.records import DETOKENISE_SERVICES, PseudonymisationRecord, Reveal
from ..outbound.sql import email_to_json


class RevealBody(BaseModel):
    token: str = Field(max_length=40, examples=["[PERSON_1]"])
    reason: str = Field(max_length=300, examples=["Verify the customer's account"])
    thread_id: str | None = Field(default=None, max_length=36)


class DetokeniseBody(BaseModel):
    text: str = Field(max_length=100_000)
    purpose: str = Field(max_length=100, examples=["customer reply"])
    thread_id: str | None = Field(default=None, max_length=36)


class NewRequestBody(BaseModel):
    type: str = Field(max_length=20, examples=["erasure"])
    address: str = Field(max_length=320, description="Compared by keyed hash; never returned")
    received_on: date | None = None


class VerifyBody(BaseModel):
    how: str = Field(max_length=300, examples=["Replied from the registered address"])


class CloseBody(BaseModel):
    outcome: str = Field(max_length=500)


def request_json(r: DataRequest, today: date) -> dict[str, Any]:
    """The address never leaves this service through this API: the screen shows the token."""
    label, article = LABELS[r.type]
    due, level = due_label(r, today)
    return {
        "id": r.id,
        "type": r.type.value,
        "type_label": label,
        "article": article,
        "status": r.status.value,
        "who": f"[{r.subject_token}]" if r.subject_token else None,
        "received_on": r.received_on.isoformat(),
        "due_on": r.due_on.isoformat(),
        "due": due,
        "due_level": level,
        "verified": r.verified_at is not None,
        "verified_how": r.verified_how,
        "pending_stores": r.pending_stores,
        "outcome": r.outcome,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }


def store_json(s: StoreReport) -> dict[str, Any]:
    return {
        "store": s.store,
        "label": s.label,
        "what": s.what,
        "count": s.count,
        "erase_action": s.erase_action,
        "available": s.available,
    }


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
        RequestNotFoundError: 404,
        StoresUnavailableError: 409,  # privacy-service itself is fine: must not trip callers' breakers
        SubjectBusyError: 409,
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

    def today() -> date:
        now: date = container.clock.now().date()
        return now

    @router.get("/dsr")
    async def requests(principal: SignedIn) -> dict[str, Any]:
        items = await container.list_requests(principal)
        return {
            "requests": [request_json(r, today()) for r in items],
            "open": sum(r.is_open for r in items),
            "retention": container.retention_schedule(),
        }

    @router.post("/dsr", status_code=201)
    async def log_request(body: NewRequestBody, principal: SignedIn) -> dict[str, Any]:
        request = await container.log_request(principal, body.type, body.address, body.received_on)
        return request_json(request, today())

    @router.get("/dsr/{dsr_id}")
    async def get_request(dsr_id: str, principal: SignedIn) -> dict[str, Any]:
        request, stores = await container.get_request(principal, dsr_id)
        return {**request_json(request, today()), "stores": [store_json(s) for s in stores]}

    @router.post("/dsr/{dsr_id}/verify")
    async def verify(dsr_id: str, body: VerifyBody, principal: SignedIn) -> dict[str, Any]:
        return request_json(await container.verify_request(principal, dsr_id, body.how), today())

    @router.post("/dsr/{dsr_id}/export")
    async def export(dsr_id: str, principal: SignedIn) -> dict[str, Any]:
        return request_json(await container.start_export(principal, dsr_id), today())

    @router.post("/dsr/{dsr_id}/erase")
    async def erase(dsr_id: str, principal: SignedIn) -> dict[str, Any]:
        return request_json(await container.start_erasure(principal, dsr_id), today())

    @router.post("/dsr/{dsr_id}/close")
    async def close(dsr_id: str, body: CloseBody, principal: SignedIn) -> dict[str, Any]:
        return request_json(await container.close_request(principal, dsr_id, body.outcome), today())

    @router.post("/dsr/{dsr_id}/retry")
    async def retry(dsr_id: str, principal: SignedIn) -> dict[str, Any]:
        return request_json(await container.retry_send(principal, dsr_id), today())

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

    @router.get("/dsr/{dsr_id}/outbound")
    async def outbound(dsr_id: str, kind: str, caller: Caller, response: Response) -> dict[str, Any]:
        """What to send, for mail-service only. Never logged or stored."""
        response.headers["Cache-Control"] = "no-store"
        if caller not in DETOKENISE_SERVICES:
            raise NotAllowedError(f"Service {caller} may not read an outgoing data request.")
        m = await container.build_outbound(dsr_id, kind)
        return {
            "to": m.to,
            "subject": m.subject,
            "body": m.body,
            "attachment_name": m.attachment_name,
            "attachment": m.attachment,
        }

    return router
