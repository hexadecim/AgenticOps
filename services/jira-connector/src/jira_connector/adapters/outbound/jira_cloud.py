"""Jira Cloud REST v3 behind the `jira` circuit breaker. 4xx = Jira refused (never retried, never trips the
breaker); 5xx/timeouts/transport = unavailable (retried later)."""

from __future__ import annotations

from typing import Any

import httpx
from opspilot_common.resilience import CircuitOpenError, ResilienceError, ResilientHttpClient

from ...domain.errors import TrackerRejectedError, TrackerUnavailableError
from ...domain.model import IssueSummary, JiraSettings, TicketFields, adf


def _reason(response: httpx.Response) -> str:
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    parts = [*body.get("errorMessages", []), *(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())]
    return "; ".join(parts) or f"HTTP {response.status_code}"


class JiraCloudTracker:
    def __init__(self, settings: JiraSettings, http: ResilientHttpClient) -> None:
        self._s, self._http = settings, http

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except CircuitOpenError as exc:
            raise TrackerUnavailableError(
                f"Jira kept failing; OpsPilot pauses calls for {exc.retry_after:.0f} s.",
                retry_after=exc.retry_after,
            ) from exc
        except ResilienceError as exc:
            raise TrackerUnavailableError(f"Jira is unavailable: {exc}") from exc
        if response.status_code in (401, 403):
            raise TrackerRejectedError(
                "Jira refused the credentials or permission (check Admin > Integrations)."
            )
        if response.status_code == 429:
            raise TrackerUnavailableError("Jira is rate limiting OpsPilot.", retry_after=60)
        if response.status_code >= 400:
            raise TrackerRejectedError(f"Jira refused the request: {_reason(response)}")
        return response

    async def _search(self, jql: str, fields: list[str], limit: int) -> list[dict[str, Any]]:
        response = await self._call(
            "POST", "/rest/api/3/search/jql", json={"jql": jql, "fields": fields, "maxResults": limit}
        )
        return list(response.json().get("issues", []))

    async def find_by_label(self, label: str) -> str | None:
        issues = await self._search(
            f'project = "{self._s.project_key}" AND labels = "{label}"', ["summary"], 1
        )
        return str(issues[0]["key"]) if issues else None

    async def create(self, fields: TicketFields, labels: list[str]) -> str:
        body = {
            "fields": {
                "project": {"key": self._s.project_key},
                "issuetype": {"name": self._s.issue_type},
                "summary": fields.summary[:240],
                "description": adf(fields.description),
                "labels": labels,
            }
        }
        # Not retried automatically: Jira has no idempotency keys. The job retries after a label search.
        response = await self._call("POST", "/rest/api/3/issue", json=body)
        return str(response.json()["key"])

    async def recent(self, hours: int) -> list[IssueSummary]:
        jql = (
            f'project = "{self._s.project_key}" AND created >= -{int(hours)}h '
            "AND statusCategory != Done ORDER BY created DESC"
        )
        issues = await self._search(jql, ["summary", "status", "created"], 50)
        return [
            IssueSummary(
                key=str(i["key"]),
                summary=str(i["fields"].get("summary", "")),
                status=str((i["fields"].get("status") or {}).get("name", "")),
                created=str(i["fields"].get("created", "")),
            )
            for i in issues
        ]

    async def comment(self, key: str, text: str) -> None:
        await self._call("POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf(text)})

    async def aclose(self) -> None:
        await self._http.aclose()
