"""Clients for privacy-service and jira-connector (service tokens, circuit breakers)."""

from __future__ import annotations

from typing import Any

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import ContextUnavailableError, PrivacyUnavailableError, StopRunError


def _headers(tokens: ServiceTokenIssuer) -> dict[str, str]:
    return {**tokens.header(), "X-Correlation-ID": correlation_id.get() or ""}


class PrivacyClient:
    """Fail closed: anything but a tokenised email is an error; there is no path to raw content."""

    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def pseudonymise(self, email_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            # privacy-service answers the same record for the same email, so retrying is safe.
            response = await self._http.post(
                f"/internal/v1/emails/{email_id}/pseudonymise",
                headers=_headers(self._tokens),
                idempotency_key=email_id,
            )
        except ResilienceError as exc:
            raise PrivacyUnavailableError(f"privacy-service is unavailable: {exc}") from exc
        body = response.json() if response.content else {}
        if response.status_code == 200:
            privacy = {
                k: body.get(k)
                for k in ("record_id", "summary", "counts", "signature_removed", "quoted_removed")
            }
            return body["email"], privacy
        if response.status_code == 404:
            raise StopRunError("The email no longer exists (deleted after the retention period).")
        if body.get("code") == "privacy_leak_blocked":
            raise StopRunError(f"Privacy check stopped this email: {body.get('message')}")
        raise PrivacyUnavailableError(
            f"privacy-service answered {response.status_code}: {body.get('message', '')}"
        )


class JiraContextClient:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def context(self, hours: int) -> dict[str, Any]:
        try:
            response = await self._http.get(
                "/internal/v1/context", params={"hours": hours}, headers=_headers(self._tokens)
            )
        except ResilienceError as exc:
            raise ContextUnavailableError(str(exc)) from exc
        if response.status_code != 200:
            message = (response.json() if response.content else {}).get("message", response.status_code)
            raise ContextUnavailableError(str(message))
        return dict(response.json())
