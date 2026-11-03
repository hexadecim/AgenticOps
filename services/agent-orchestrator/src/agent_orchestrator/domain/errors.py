"""Why a run cannot continue right now. Parking errors are retried; the others stop the run."""

from __future__ import annotations


class AgentError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LlmUnavailableError(AgentError):
    """The local model cannot be reached, timed out, or its circuit is open: park as `waiting_llm`."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PrivacyUnavailableError(AgentError):
    """privacy-service cannot pseudonymise right now. Fail closed: park, never read raw mail instead."""


class StopRunError(AgentError):
    """Nothing to retry: the email is gone, privacy blocked it, or the model keeps answering nonsense."""


class ContextUnavailableError(AgentError):
    """jira-connector could not search; duplicate detection is advisory, so the run continues without it."""


class OpsUnavailableError(AgentError):
    """ops-service cannot give evidence right now: park as `waiting_ops`. No fix is proposed or verified
    without fresh evidence."""


class DiagnosisUnusableError(AgentError):
    """The model's fix choice could not be read even after a retry: the run ends without a proposal."""
