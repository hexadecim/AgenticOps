"""audit-service and privacy-service clients, each behind its own circuit breaker."""

from __future__ import annotations

from ...ports import Json
from .service_http import ServiceHttpClient


class AuditHttpClient(ServiceHttpClient):
    async def entries(self, token: str, params: dict[str, str]) -> Json:
        return await self._obj("GET", "/v1/entries", token=token, params=params)


class PrivacyHttpClient(ServiceHttpClient):
    async def reveal(self, token: str, body: Json) -> Json:
        # Not retried: every reveal is an audited access.
        return await self._obj("POST", "/v1/reveals", token=token, json=body)

    async def data_requests(self, token: str) -> Json:
        return await self._obj("GET", "/v1/dsr", token=token)

    async def log_data_request(self, token: str, body: Json) -> Json:
        return await self._obj("POST", "/v1/dsr", token=token, json=body)

    async def data_request(self, token: str, dsr_id: str) -> Json:
        return await self._obj("GET", f"/v1/dsr/{dsr_id}", token=token)

    async def data_request_step(self, token: str, dsr_id: str, step: str, body: Json | None) -> Json:
        return await self._obj("POST", f"/v1/dsr/{dsr_id}/{step}", token=token, json=body)
