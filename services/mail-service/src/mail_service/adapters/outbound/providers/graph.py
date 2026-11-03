"""Microsoft Graph (v1.0) with application permissions. Lists by `receivedDateTime`, downloads each message as
MIME (`/$value`) for the shared parser, and sends MIME through `sendMail`. Never modifies the mailbox.

Read position: `since` (ISO time of the newest message read) plus the ids already read at that instant.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....domain.errors import MailboxRejectedError
from ....domain.mail import FetchBatch, IncomingMail, MailboxConfig, OutgoingMail
from ._errors import unavailable
from ._oauth import AccessToken
from .mime import build_mime, parse_mime

GRAPH = "https://graph.microsoft.com/v1.0"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphMailbox:
    """MailReader + MailSender for Microsoft 365."""

    can_send = True

    def __init__(self, config: MailboxConfig, http: ResilientHttpClient) -> None:
        self._http, self._folder, self._address = http, config.folder, config.address
        self._user = f"{GRAPH}/users/{quote(config.address)}"
        tenant = quote(str(config.values.get("graph_tenant_id", "")))
        self._token = AccessToken(
            http,
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            {
                "grant_type": "client_credentials",
                "client_id": str(config.values.get("graph_client_id", "")),
                "client_secret": config.secrets.get("graph_client_secret", ""),
                "scope": "https://graph.microsoft.com/.default",
            },
            "Microsoft",
        )
        self._folder_id: str | None = None

    async def _get(self, url: str, **params: Any) -> httpx.Response:
        response = await self._http.get(url, params=params or None, headers=await self._token.headers())
        if response.status_code in (401, 403):
            self._token.invalidate()
            raise MailboxRejectedError(
                "Microsoft refused access. The app needs Mail.ReadWrite with admin consent."
            )
        if response.status_code == 404:
            raise MailboxRejectedError(f"Mailbox or folder '{self._folder}' not found.")
        return response

    async def _folder_path(self) -> str:
        if self._folder_id is None:
            if self._folder.upper() == "INBOX":
                self._folder_id = "inbox"
            else:
                name = self._folder.replace("'", "''")
                found = (
                    await self._get(f"{self._user}/mailFolders", **{"$filter": f"displayName eq '{name}'"})
                ).json()
                if not found.get("value"):
                    raise MailboxRejectedError(f"There is no mail folder named '{self._folder}'.")
                self._folder_id = str(found["value"][0]["id"])
        return f"{self._user}/mailFolders/{self._folder_id}"

    async def fetch_new(self, cursor: Mapping[str, Any] | None, *, limit: int, since: datetime) -> FetchBatch:
        try:
            return await self._fetch(cursor, limit, since)
        except ResilienceError as exc:
            raise unavailable(exc, "Microsoft 365") from exc

    async def _fetch(self, cursor: Mapping[str, Any] | None, limit: int, since: datetime) -> FetchBatch:
        start = str(cursor["since"]) if cursor else _iso(since)
        seen = set(cursor.get("seen", [])) if cursor else set()
        listed = (
            await self._get(
                f"{await self._folder_path()}/messages",
                **{
                    "$filter": f"receivedDateTime ge {start}",
                    "$orderby": "receivedDateTime asc",
                    "$select": "id,conversationId,receivedDateTime",
                    "$top": str(limit + len(seen) + 1),
                },
            )
        ).json()
        pending = [m for m in listed.get("value", []) if m["id"] not in seen]
        batch, more = pending[:limit], len(pending) > limit or bool(listed.get("@odata.nextLink"))
        mails: list[IncomingMail] = []
        for item in batch:
            response = await self._http.get(
                f"{self._user}/messages/{quote(item['id'])}/$value", headers=await self._token.headers()
            )
            if response.status_code == 404:
                continue
            mails.append(
                parse_mime(
                    response.content,
                    provider_id=item["id"],
                    provider_thread_id=item.get("conversationId"),
                    received_at=_parse_iso(item["receivedDateTime"]),
                )
            )
        if batch:
            newest = max(_parse_iso(m["receivedDateTime"]) for m in batch)
            at_newest = {m["id"] for m in batch if _parse_iso(m["receivedDateTime"]) == newest}
            seen = (seen | at_newest) if _iso(newest) == start else at_newest
            start = _iso(newest)
        return FetchBatch(mails, {"since": start, "seen": sorted(seen)}, more and bool(batch))

    async def send(self, mail: OutgoingMail) -> str:
        raw, message_id = build_mime(mail, self._address)
        try:
            response = await self._http.post(
                f"{self._user}/sendMail",
                content=base64.b64encode(raw),
                headers={**await self._token.headers(), "Content-Type": "text/plain"},
            )
        except ResilienceError as exc:
            raise unavailable(exc, "Microsoft 365") from exc
        if response.status_code >= 400:
            raise MailboxRejectedError(f"Microsoft refused to send the message ({response.status_code}).")
        return message_id
