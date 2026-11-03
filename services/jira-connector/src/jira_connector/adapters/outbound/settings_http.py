"""Reads the resolved Jira settings from settings-service's internal API with a service token."""

from __future__ import annotations

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import SettingsUnavailableError
from ...domain.model import JiraSettings


class SettingsServiceSource:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def jira(self) -> JiraSettings | None:
        try:
            response = await self._http.get(
                "/internal/v1/sections/jira/resolved",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
            )
        except ResilienceError as exc:
            raise SettingsUnavailableError(f"settings-service is unavailable: {exc}") from exc
        if response.status_code != 200:
            raise SettingsUnavailableError(f"settings-service refused the request ({response.status_code})")
        body = response.json()
        if not body.get("configured"):
            return None
        v = body["values"]
        return JiraSettings(
            version=int(body["version"]),
            base_url=str(v["base_url"]).rstrip("/"),
            email=str(v["email"]),
            project_key=str(v["project_key"]),
            issue_type=str(v.get("issue_type") or "Incident"),
            api_token=str(body.get("secrets", {}).get("api_token", "")),
        )
