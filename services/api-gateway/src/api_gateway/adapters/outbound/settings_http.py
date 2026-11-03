"""settings-service client, behind the `settings-service` circuit breaker."""

from __future__ import annotations

from ...ports import Json
from .service_http import ServiceHttpClient


class SettingsHttpClient(ServiceHttpClient):
    async def list_sections(self, token: str) -> list[Json]:
        return await self._list("GET", "/v1/sections", token=token)

    async def get_section(self, token: str, section: str) -> Json:
        return await self._obj("GET", f"/v1/sections/{section}", token=token)

    async def save_section(self, token: str, section: str, body: Json) -> Json:
        return await self._obj("PUT", f"/v1/sections/{section}", token=token, json=body)

    async def test_section(self, token: str, section: str, body: Json) -> Json:
        return await self._obj("POST", f"/v1/sections/{section}/test", token=token, json=body)
