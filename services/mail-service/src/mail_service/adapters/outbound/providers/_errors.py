from __future__ import annotations

from opspilot_common.resilience import (
    CircuitOpenError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ResilienceError,
)

from ....domain.errors import MailboxUnavailableError


def unavailable(exc: ResilienceError, what: str) -> MailboxUnavailableError:
    """Translate a resilience-layer failure into the core's error, with a plain-language reason."""
    if isinstance(exc, CircuitOpenError):
        return MailboxUnavailableError(
            f"{what} kept failing; OpsPilot pauses calls for {exc.retry_after:.0f} s.",
            retry_after=exc.retry_after,
        )
    if isinstance(exc, DependencyTimeoutError):
        return MailboxUnavailableError(f"{what} did not answer within {exc.timeout:.0f} s.")
    if isinstance(exc, DependencyUnavailableError):
        return MailboxUnavailableError(f"{what} answered with a server error ({exc.status_code}).")
    return MailboxUnavailableError(f"Cannot reach {what}.")
