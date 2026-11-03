from __future__ import annotations

from datetime import UTC, datetime

from jira_connector.domain.errors import TrackerRejectedError, TrackerUnavailableError
from jira_connector.domain.model import IssueSummary, JiraSettings, TicketFields

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
SETTINGS = JiraSettings(1, "https://team.atlassian.net", "bot@example.com", "KAN", "Incident", "token-123")


class FakeTracker:
    def __init__(self) -> None:
        self.issues: dict[str, tuple[TicketFields, list[str]]] = {}
        self.comments: list[tuple[str, str]] = []
        self.fail: Exception | None = None
        self.closed = False

    async def find_by_label(self, label: str) -> str | None:
        if self.fail:
            raise self.fail
        return next((k for k, (_, labels) in self.issues.items() if label in labels), None)

    async def create(self, fields: TicketFields, labels: list[str]) -> str:
        if self.fail:
            raise self.fail
        key = f"KAN-{len(self.issues) + 1}"
        self.issues[key] = (fields, labels)
        return key

    async def recent(self, hours: int) -> list[IssueSummary]:
        if self.fail:
            raise self.fail
        return [IssueSummary(k, f.summary, "To Do", "2026-10-08") for k, (f, _) in self.issues.items()]

    async def comment(self, key: str, text: str) -> None:
        if self.fail:
            raise self.fail
        self.comments.append((key, text))

    async def aclose(self) -> None:
        self.closed = True


class FakeSource:
    def __init__(self, settings: JiraSettings | None = SETTINGS) -> None:
        self.settings = settings

    async def jira(self) -> JiraSettings | None:
        return self.settings


__all__ = ["NOW", "SETTINGS", "FakeSource", "FakeTracker", "TrackerRejectedError", "TrackerUnavailableError"]
