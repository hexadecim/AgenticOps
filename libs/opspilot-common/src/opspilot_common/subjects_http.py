"""The inbound side of the subject-store contract, shared by every store: one internal route, one consumer."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import ColumnElement, String, cast, false, or_
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_common.events.catalogue import SubjectErasureRequested, SubjectStoreErased
from opspilot_common.events.envelope import EventEnvelope
from opspilot_common.logging import correlation_id

from .events import parse_payload
from .subjects import StoreReport, SubjectQuery, SubjectReporter, SubjectStore

ERASURE_BINDINGS = ["subject.erasure_requested"]


def subject_router(store: SubjectReporter, require_privacy: Callable[..., Awaitable[str]]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal"])

    @router.post("/subject-data")
    async def subject_data(
        query: SubjectQuery, _: Annotated[str, Depends(require_privacy)], response: Response
    ) -> StoreReport:
        response.headers["Cache-Control"] = "no-store"
        return await store.report(query)

    return router


def erasure_handler(store: SubjectStore) -> Callable[[AsyncSession, EventEnvelope], Awaitable[None]]:
    async def handle(_: AsyncSession, envelope: EventEnvelope) -> None:
        payload = parse_payload(envelope)
        if isinstance(payload, SubjectErasureRequested):
            await store.erase(payload)

    return handle


def mentions(column: Any, tokens: list[str]) -> ColumnElement[bool]:
    """Rows whose text or JSON holds one of the tokens, bare or in brackets ("PERSON_1", [PERSON_1])."""
    if not tokens:
        return false()
    text = cast(column, String)
    return or_(
        *[text.contains(f'"{t}"', autoescape=True) for t in tokens],
        *[text.contains(f"[{t}]", autoescape=True) for t in tokens],
    )


def erased_envelope(
    service: str, request: SubjectErasureRequested, store: str, count: int, detail: str | None = None
) -> EventEnvelope:
    return EventEnvelope.wrap(
        SubjectStoreErased(dsr_id=request.dsr_id, store=store, count=count, detail=detail),
        source=service,
        actor=service,
        correlation_id=correlation_id.get(),
    )
