"""Reads the resolved mailbox settings from settings-service's internal API with a service token."""

from __future__ import annotations

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import SettingsUnavailableError
from ...domain.mail import MailboxConfig


class SettingsServiceSource:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def mailbox(self) -> MailboxConfig | None:
        try:
            response = await self._http.get(
                "/internal/v1/sections/mailbox/resolved",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
            )
        except ResilienceError as exc:
            raise SettingsUnavailableError(f"settings-service is unavailable: {exc}") from exc
        if response.status_code != 200:
            raise SettingsUnavailableError(f"settings-service refused the request ({response.status_code})")
        body = response.json()
        if not body.get("configured"):
            return None
        values = body["values"]
        return MailboxConfig(
            version=int(body["version"]),
            provider=str(values.get("provider", "imap")),
            folder=str(values.get("folder", "INBOX")),
            poll_interval_seconds=int(values.get("poll_interval_seconds", 120)),
            values=values,
            secrets=body.get("secrets", {}),
        )
