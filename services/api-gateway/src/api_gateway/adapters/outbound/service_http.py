"""Base for calling OpsPilot services: forwards the user's session as Bearer token and the correlation id,
maps 4xx to DownstreamError (passed through to the browser). Transport failures surface as ResilienceError."""

from __future__ import annotations

from typing import Any, cast

import httpx
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilientHttpClient

from ...ports import DownstreamError, Json


class ServiceHttpClient:
    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def _obj(self, method: str, path: str, **kwargs: Any) -> Json:
        return cast(Json, await self._call(method, path, **kwargs))

    async def _list(self, method: str, path: str, **kwargs: Any) -> list[Json]:
        return cast(list[Json], await self._call(method, path, **kwargs))

    async def _call(self, method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        headers = {"X-Correlation-ID": correlation_id.get() or ""}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response: httpx.Response = await self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"code": "error", "message": response.text[:200]}
            if "code" not in body:  # FastAPI validation errors → one readable message
                body = {"code": "invalid_request", "message": _validation_message(body)}
            raise DownstreamError(response.status_code, body)
        return None if response.status_code == 204 else response.json()


def _validation_message(body: Json) -> str:
    detail = body.get("detail")
    if isinstance(detail, list) and detail:
        first = detail[0]
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
        return message if message[:1].isupper() else f"{field}: {message}"
    return str(detail or "Invalid request")
