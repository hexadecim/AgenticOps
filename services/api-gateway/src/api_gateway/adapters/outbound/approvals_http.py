"""approval-service, agent-orchestrator and reporting-service clients, each behind its own circuit breaker."""

from __future__ import annotations

from ...ports import Json
from .service_http import ServiceHttpClient


class ApprovalHttpClient(ServiceHttpClient):
    async def queue(self, token: str, params: dict[str, str]) -> Json:
        return await self._obj("GET", "/v1/approvals", token=token, params=params)

    async def get(self, token: str, approval_id: str) -> Json:
        return await self._obj("GET", f"/v1/approvals/{approval_id}", token=token)

    async def decide(self, token: str, approval_id: str, body: Json) -> Json:
        # Not retried automatically; a repeat is harmless anyway (approval-service treats it as idempotent).
        return await self._obj("POST", f"/v1/approvals/{approval_id}/decision", token=token, json=body)


class RunsHttpClient(ServiceHttpClient):
    async def summary(self, token: str) -> Json:
        return await self._obj("GET", "/v1/runs/summary", token=token)


class ReportingHttpClient(ServiceHttpClient):
    async def dashboard(self, token: str) -> Json:
        return await self._obj("GET", "/v1/dashboard", token=token)

    async def timeline(self, token: str, key: str) -> Json:
        return await self._obj("GET", f"/v1/incidents/{key}/timeline", token=token)
