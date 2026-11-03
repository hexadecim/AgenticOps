from __future__ import annotations

from typing import Any

from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....ports import Check, ConnectionReport
from ._common import explain

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailTester:
    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        checks: list[Check] = []
        try:
            token = await self._http.post(
                TOKEN_URL,
                idempotency_key=None,
                data={
                    "grant_type": "refresh_token",
                    "client_id": values["gmail_client_id"],
                    "client_secret": secrets["gmail_client_secret"],
                    "refresh_token": secrets["gmail_refresh_token"],
                },
            )
            if token.status_code != 200:
                reason = token.json().get("error_description") or token.json().get("error", "rejected")
                return ConnectionReport.from_checks(
                    [
                        Check(
                            "Google sign-in",
                            False,
                            f"Google rejected the OAuth client or refresh token: {reason}",
                        )
                    ],
                    "",
                )
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            checks.append(Check("Google sign-in", True, "OAuth refresh token accepted"))
            profile = await self._http.get(f"{API}/profile", headers=headers)
            if profile.status_code == 403:
                return ConnectionReport.from_checks(
                    [
                        *checks,
                        Check(
                            "Mailbox", False, "The token lacks Gmail access. Grant the gmail.modify scope."
                        ),
                    ],
                    "",
                )
            address = profile.json().get("emailAddress", "")
            same = address.lower() == values["gmail_user"].lower()
            checks.append(
                Check(
                    "Mailbox",
                    same,
                    f"Connected to {address}"
                    if same
                    else f"The token belongs to {address}, not {values['gmail_user']}.",
                )
            )
            labels = (await self._http.get(f"{API}/labels", headers=headers)).json().get("labels", [])
            folder = values["folder"]
            found = any(
                lbl.get("name", "").lower() == folder.lower() or lbl.get("id") == folder.upper()
                for lbl in labels
            )
            checks.append(
                Check("Folder", found, f"{folder} found" if found else f"No Gmail label named {folder}.")
            )
        except ResilienceError as exc:
            checks.append(Check("Reach Google", False, explain(exc, "Google")))
        return ConnectionReport.from_checks(checks, f"OpsPilot can read {values['gmail_user']}.")
