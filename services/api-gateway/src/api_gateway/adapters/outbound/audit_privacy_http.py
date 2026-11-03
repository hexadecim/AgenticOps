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
