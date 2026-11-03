from __future__ import annotations

from typing import Any

import httpx
from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....ports import Check, ConnectionReport
from ._common import explain


class JiraTester:
    """Checks credentials, project, issue type and the permission to create issues."""

    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        base, key = values["base_url"], values["project_key"].upper()
        auth = httpx.BasicAuth(values["email"], secrets["api_token"])
        checks: list[Check] = []
        try:
            me = await self._http.get(f"{base}/rest/api/3/myself", auth=auth)
            if me.status_code in (401, 403):
                return _done(checks, Check("Sign in", False, "Jira rejected the account email or API token."))
            if me.status_code == 404 or "json" not in me.headers.get("content-type", ""):
                return _done(checks, Check("Sign in", False, f"No Jira Cloud site answered at {base}."))
            checks.append(
                Check("Sign in", True, f"Signed in as {me.json().get('displayName', values['email'])}")
            )

            project = await self._http.get(f"{base}/rest/api/3/project/{key}", auth=auth)
            if project.status_code == 404:
                return _done(
                    checks,
                    Check("Project", False, f"Project {key} does not exist or this account cannot see it."),
                )
            body = project.json()
            checks.append(Check("Project", True, f"{key} · {body.get('name', '')}"))

            types = [t["name"] for t in body.get("issueTypes", []) if not t.get("subtask")]
            wanted = values["issue_type"]
            has_type = any(t.lower() == wanted.lower() for t in types)
            checks.append(
                Check(
                    "Issue type",
                    has_type,
                    f"{wanted} is available"
                    if has_type
                    else f"{key} has no issue type '{wanted}'. Available: {', '.join(types) or 'none'}.",
                )
            )

            perms = await self._http.get(
                f"{base}/rest/api/3/mypermissions",
                auth=auth,
                params={"projectKey": key, "permissions": "CREATE_ISSUES"},
            )
            can_create = bool(
                perms.json().get("permissions", {}).get("CREATE_ISSUES", {}).get("havePermission")
            )
            checks.append(
                Check(
                    "Create issues",
                    can_create,
                    "Allowed" if can_create else f"This account cannot create issues in {key}.",
                )
            )
        except ResilienceError as exc:
            checks.append(Check("Reach Jira", False, explain(exc, f"Jira at {base}")))
        return ConnectionReport.from_checks(
            checks, f"Ready to create {values['issue_type']} issues in {key}."
        )


def _done(checks: list[Check], failed: Check) -> ConnectionReport:
    return ConnectionReport.from_checks([*checks, failed], "")
