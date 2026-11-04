"""Turns tokens back into real values through privacy-service (service token, circuit breaker). Only for
sending an approved message; the result is never stored or logged."""

from __future__ import annotations

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ...domain.errors import DetokeniseRefusedError, DetokeniseUnavailableError
from ...domain.mail import Address, OutgoingAttachment, OutgoingMail


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


class PrivacyDsrContent:
    """What to send for a data request, and to whom: privacy-service decides both. Never stored or logged."""

    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def outbound(self, dsr_id: str, kind: str) -> OutgoingMail:
        try:
            response = await self._http.get(
                f"/internal/v1/dsr/{dsr_id}/outbound",
                params={"kind": kind},
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
            )
        except ResilienceError as exc:
            raise DetokeniseUnavailableError(f"privacy-service is unavailable: {exc}") from exc
        body = response.json() if response.content else {}
        if response.status_code >= 500 or body.get("code") == "stores_unavailable":
            raise DetokeniseUnavailableError(str(body.get("message") or "privacy-service is unavailable"))
        if response.status_code != 200:
            raise DetokeniseRefusedError(
                str(body.get("message") or f"privacy-service answered {response.status_code}")
            )
        files: tuple[OutgoingAttachment, ...] = ()
        if body.get("attachment_name"):
            files = (OutgoingAttachment(str(body["attachment_name"]), str(body["attachment"])),)
        return OutgoingMail(
            to=(Address(str(body["to"]), ""),),
            subject=str(body["subject"]),
            body_text=str(body["body"]),
            attachments=files,
        )
