from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from opspilot_common.resilience import ResilientHttpClient

from ....domain.errors import MailboxRejectedError


class AccessToken:
    """Fetches an OAuth access token and reuses it until a minute before it expires."""

    def __init__(
        self,
        http: ResilientHttpClient,
        url: str,
        form: dict[str, str],
        who: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http, self._url, self._form, self._who, self._clock = http, url, form, who, clock
        self._token, self._expires = "", 0.0

    def invalidate(self) -> None:
        self._expires = 0.0

    async def headers(self) -> dict[str, str]:
        if self._clock() >= self._expires - 60:
            response = await self._http.post(self._url, data=self._form)
            body: dict[str, Any] = response.json() if response.content else {}
            if response.status_code != 200:
                reason = str(body.get("error_description") or body.get("error") or response.status_code)
                raise MailboxRejectedError(f"{self._who} rejected the credentials: {reason.splitlines()[0]}")
            self._token = str(body["access_token"])
            self._expires = self._clock() + float(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}
