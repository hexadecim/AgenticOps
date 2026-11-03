"""Reads raw email from mail-service's internal API with a service token. Any failure → fail closed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.entities import Identity
from ...domain.errors import EmailNotFoundError, PrivacyUnavailableError
from ...ports import RawEmail


def _identity(d: dict[str, Any]) -> Identity:
    return Identity(str(d.get("email", "")), str(d.get("name", "")))


class MailServiceSource:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def get(self, email_id: str) -> RawEmail:
        try:
            response = await self._http.get(
                f"/internal/v1/emails/{email_id}",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
            )
        except ResilienceError as exc:
            raise PrivacyUnavailableError(f"mail-service is unavailable: {exc}") from exc
        if response.status_code == 404:
            raise EmailNotFoundError(email_id)
        if response.status_code != 200:
            raise PrivacyUnavailableError(f"mail-service refused the request ({response.status_code})")
        d = response.json()
        return RawEmail(
            email_id=d["id"],
            thread_id=d["thread_id"],
            sender=_identity(d["from"]),
            to=tuple(_identity(a) for a in d.get("to", [])),
            cc=tuple(_identity(a) for a in d.get("cc", [])),
            subject=d.get("subject", ""),
            body=d.get("body_text", ""),
            received_at=datetime.fromisoformat(d["received_at"]),
            attachments=tuple(d.get("attachments", [])),
        )
