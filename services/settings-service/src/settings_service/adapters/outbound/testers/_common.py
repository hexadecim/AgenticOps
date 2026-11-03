from __future__ import annotations

from opspilot_common.resilience import (
    CircuitOpenError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ResilienceError,
)


def explain(exc: ResilienceError, what: str) -> str:
    """Plain-language reason for a failed call to `what`."""
    if isinstance(exc, CircuitOpenError):
        wait = f"{exc.retry_after:.0f} s"
        return f"Recent attempts to reach {what} kept failing; OpsPilot is pausing calls for {wait}."
    if isinstance(exc, DependencyTimeoutError):
        return f"{what} did not answer within {exc.timeout:.0f} seconds."
    if isinstance(exc, DependencyUnavailableError):
        return f"{what} answered with a server error ({exc.status_code}). Try again later."
    return f"Cannot reach {what}. Check the address and that it is running."
