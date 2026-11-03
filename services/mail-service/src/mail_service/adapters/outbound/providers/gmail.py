"""Gmail API (REST v1). Reads with `format=raw` so the shared MIME parser applies; never modifies labels.

Read position: `after` (epoch seconds, Gmail's search granularity) plus the ids already read in that second.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....domain.errors import MailboxRejectedError
from ....domain.mail import FetchBatch, IncomingMail, MailboxConfig, OutgoingMail
from ._errors import unavailable
from ._oauth import AccessToken
from .mime import build_mime, parse_mime

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SYSTEM_LABELS = frozenset({"INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT"})
MAX_LISTED = 2000


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class GmailMailbox:
    """MailReader + MailSender for Google Workspace / Gmail."""

    can_send = True

    def __init__(self, config: MailboxConfig, http: ResilientHttpClient) -> None:
        self._http, self._folder, self._address = http, config.folder, config.address
        self._token = AccessToken(
            http,
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": str(config.values.get("gmail_client_id", "")),
                "client_secret": config.secrets.get("gmail_client_secret", ""),
                "refresh_token": config.secrets.get("gmail_refresh_token", ""),
            },
            "Google",
        )
        self._label: str | None = None

    async def _get(self, url: str, **params: Any) -> httpx.Response:
        response = await self._http.get(url, params=params, headers=await self._token.headers())
        if response.status_code in (401, 403):
            self._token.invalidate()
            raise MailboxRejectedError("Google refused access to the mailbox. Check the gmail.modify scope.")
        return response

    async def _label_id(self) -> str:
        if self._label is None:
            if self._folder.upper() in SYSTEM_LABELS:
                self._label = self._folder.upper()
            else:
                labels = (await self._get(f"{API}/labels")).json().get("labels", [])
                match = next(
                    (lb for lb in labels if lb.get("name", "").lower() == self._folder.lower()), None
                )
                if match is None:
                    raise MailboxRejectedError(f"There is no Gmail label named '{self._folder}'.")
                self._label = str(match["id"])
        return self._label

    async def fetch_new(self, cursor: Mapping[str, Any] | None, *, limit: int, since: datetime) -> FetchBatch:
        try:
            return await self._fetch(cursor, limit, since)
        except ResilienceError as exc:
            raise unavailable(exc, "Gmail") from exc

    async def _fetch(self, cursor: Mapping[str, Any] | None, limit: int, since: datetime) -> FetchBatch:
        after = int(cursor["after"]) if cursor else int(since.timestamp())
        seen = set(cursor.get("seen", [])) if cursor else set()
        label, page = await self._label_id(), None
        ids: list[str] = []
        while len(ids) < MAX_LISTED:
            params: dict[str, Any] = {"labelIds": label, "q": f"after:{after}", "maxResults": 500}
            if page:
                params["pageToken"] = page
            body = (await self._get(f"{API}/messages", **params)).json()
            ids += [m["id"] for m in body.get("messages", [])]
            page = body.get("nextPageToken")
            if not page:
                break
        pending = [i for i in reversed(ids) if i not in seen]  # Gmail lists newest first
        batch, more = pending[:limit], len(pending) > limit
        mails: list[IncomingMail] = []
        for message_id in batch:
            response = await self._get(f"{API}/messages/{message_id}", format="raw")
            if response.status_code == 404:
                continue  # deleted between list and get
            data = response.json()
            mails.append(
                parse_mime(
                    _b64decode(data["raw"]),
                    provider_id=message_id,
                    provider_thread_id=data.get("threadId"),
                    received_at=datetime.fromtimestamp(int(data["internalDate"]) / 1000, UTC),
                )
            )
        if mails:
            newest = max(int(m.received_at.timestamp()) for m in mails)
            same_second = {m.provider_id for m in mails if int(m.received_at.timestamp()) == newest}
            seen = (seen | same_second) if newest == after else same_second
            after = newest
        return FetchBatch(mails, {"after": after, "seen": sorted(seen)}, more)

    async def send(self, mail: OutgoingMail) -> str:
        raw, message_id = build_mime(mail, self._address)
        try:
            response = await self._http.post(
                f"{API}/messages/send",
                json={"raw": base64.urlsafe_b64encode(raw).decode()},
                headers=await self._token.headers(),
            )
        except ResilienceError as exc:
            raise unavailable(exc, "Gmail") from exc
        if response.status_code >= 400:
            raise MailboxRejectedError(f"Gmail refused to send the message ({response.status_code}).")
        return message_id
