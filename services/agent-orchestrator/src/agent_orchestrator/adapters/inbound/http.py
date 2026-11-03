"""Run status for the console (through the gateway). Content stays in the approvals; this is counts only.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from opspilot_common.auth import Principal


def build_router(container: Any) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["runs"])
    SignedIn = Annotated[Principal, Depends(container.current_user)]

    @router.get("/runs/summary")
    async def summary(_: SignedIn) -> dict[str, Any]:
        counts = await container.run_summary()
        llm = await container.llm_circuit.retry_after()
        return {
            "counts": counts,
            "ai_paused": counts["waiting_llm"] > 0 or llm is not None,
            "ai_retry_after_seconds": round(llm) if llm else None,
            "in_progress": counts["queued"] + counts["running"] + counts["resuming"],
        }

    return router
