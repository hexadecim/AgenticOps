"""ops-service errors. Adapters translate driver and resilience errors into these."""

from __future__ import annotations


class OpsError(Exception):
    code = "ops_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(OpsError):
    code = "not_found"


class NotAllowedError(OpsError):
    code = "not_allowed"


class ActionRefusedError(OpsError):
    """The action or a parameter is not on the allow-list: nothing runs."""

    code = "action_refused"


class EvidenceUnavailableError(OpsError):
    """Probes could not be run, so there is no fresh evidence. A fix never runs without it."""

    code = "evidence_unavailable"


class LogsUnavailableError(OpsError):
    """The log folder is missing or holds no log files: diagnostics go on without logs and say so."""

    code = "logs_unavailable"


class ExecutorUnavailableError(OpsError):
    """The executor could not be reached; the job is retried later."""

    code = "executor_unavailable"


class EmbeddingUnavailableError(OpsError):
    """The embedding model is down; runbook search falls back to keywords."""

    code = "embedding_unavailable"


class SettingsUnavailableError(OpsError):
    code = "settings_unavailable"
