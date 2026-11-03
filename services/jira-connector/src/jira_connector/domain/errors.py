from __future__ import annotations


class JiraError(Exception):
    code = "jira_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TrackerUnavailableError(JiraError):
    """Jira could not be reached, timed out, failed, or its circuit is open: try again later."""

    code = "jira_unavailable"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TrackerRejectedError(JiraError):
    """Jira answered and refused (bad credentials, missing permission, unknown project or field)."""

    code = "jira_rejected"


class SettingsUnavailableError(JiraError):
    code = "settings_unavailable"
