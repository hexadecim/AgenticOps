from __future__ import annotations

from typing import Any
from urllib.parse import quote

from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....ports import Check, ConnectionReport
from ._common import explain

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphTester:
    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        tenant, mailbox = values["graph_tenant_id"], values["graph_mailbox"]
        checks: list[Check] = []
        try:
            token = await self._http.post(
                f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": values["graph_client_id"],
                    "client_secret": secrets["graph_client_secret"],
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            if token.status_code != 200:
                reason = token.json().get("error_description", "rejected").split("\r\n")[0]
                return ConnectionReport.from_checks(
                    [Check("Microsoft sign-in", False, f"Microsoft rejected the app registration: {reason}")],
                    "",
                )
            checks.append(Check("Microsoft sign-in", True, "App registration accepted"))
            folder = "inbox" if values["folder"].upper() == "INBOX" else quote(values["folder"])
            response = await self._http.get(
                f"{GRAPH}/users/{quote(mailbox)}/mailFolders/{folder}",
                headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            )
            if response.status_code == 403:
                checks.append(
                    Check(
                        "Mailbox",
                        False,
                        "The app lacks Mail.ReadWrite permission, or admin consent has not been granted.",
                    )
                )
            elif response.status_code == 404:
                checks.append(
                    Check("Mailbox", False, f"Mailbox {mailbox} or folder {values['folder']} not found.")
                )
            else:
                total = response.json().get("totalItemCount", "?")
                checks.append(Check("Mailbox", True, f"{mailbox} · {values['folder']} has {total} messages"))
        except ResilienceError as exc:
            checks.append(Check("Reach Microsoft", False, explain(exc, "Microsoft 365")))
        return ConnectionReport.from_checks(checks, f"OpsPilot can read {mailbox}.")
