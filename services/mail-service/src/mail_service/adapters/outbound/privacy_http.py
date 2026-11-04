"""Turns tokens back into real values through privacy-service (service token, circuit breaker). Only for
sending an approved message; the result is never stored or logged."""

from __future__ import annotations

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import DetokeniseRefusedError, DetokeniseUnavailableError


class PrivacyDetokeniser:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def detokenise(self, text: str, *, thread_id: str) -> str:
        try:
            response = await self._http.post(
                "/internal/v1/detokenise",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
                json={"text": text, "purpose": "customer reply", "thread_id": thread_id},
            )
        except ResilienceError as exc:
            raise DetokeniseUnavailableError(f"privacy-service is unavailable: {exc}") from exc
        body = response.json() if response.content else {}
        if response.status_code == 200:
            return str(body["text"])
        message = str(body.get("message") or f"privacy-service answered {response.status_code}")
        if response.status_code >= 500:
            raise DetokeniseUnavailableError(message)
        raise DetokeniseRefusedError(message)
