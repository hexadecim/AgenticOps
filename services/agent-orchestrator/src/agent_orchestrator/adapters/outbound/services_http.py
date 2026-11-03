"""Clients for privacy-service, jira-connector and ops-service (service tokens, circuit breakers)."""

from __future__ import annotations

from typing import Any

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import (
    ContextUnavailableError,
    OpsUnavailableError,
    PrivacyUnavailableError,
    StopRunError,
)


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


class OpsClient:
    """Evidence for diagnostics and the health re-check. Unavailable ⇒ the run waits: no fix is proposed or
    declared working without fresh evidence."""

    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, headers=_headers(self._tokens), **kwargs)
        except ResilienceError as exc:
            raise OpsUnavailableError(f"ops-service is unavailable: {exc}") from exc
        body = response.json() if response.content else {}
        if response.status_code != 200:
            raise OpsUnavailableError(
                f"ops-service answered {response.status_code}: {body.get('message', '')}"
            )
        return dict(body)

    async def diagnose(
        self, *, text: str, site: str | None, asset: str | None, run_id: str, thread_id: str
    ) -> dict[str, Any]:
        body = {"text": text[:4000], "site": site, "asset": asset, "run_id": run_id, "thread_id": thread_id}
        # Reading evidence has no side effect beyond probe events, so a retry is safe.
        return await self._call("POST", "/internal/v1/diagnostics", json=body, idempotency_key=run_id)

    async def probes(self, region: str | None, *, run_id: str, thread_id: str) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("region", region), ("run_id", run_id), ("thread_id", thread_id)) if v}
        return list((await self._call("GET", "/internal/v1/probes", params=params)).get("probes", []))
