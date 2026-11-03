"""mail-service client, behind the `mail-service` circuit breaker."""

from __future__ import annotations

from ...ports import Json
from .service_http import ServiceHttpClient


class MailHttpClient(ServiceHttpClient):
    async def status(self, token: str) -> Json:
        return await self._obj("GET", "/v1/status", token=token)

    async def poll(self, token: str) -> Json:
        return await self._obj("POST", "/v1/poll", token=token)
