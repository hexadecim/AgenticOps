"""Which exceptions count against a dependency's health, and which are worth retrying.

Only server-side trouble trips a breaker: timeouts, transport errors and 5xx. A 4xx means the caller sent
a bad request; the dependency is healthy, so it must neither open the circuit nor be retried.
"""

from collections.abc import Callable

import httpx

from .errors import DependencyTimeoutError, DependencyUnavailableError, DependencyUnreachableError

FailurePolicy = Callable[[BaseException], bool]

_FAILURE_TYPES: tuple[type[BaseException], ...] = (
    DependencyTimeoutError,
    DependencyUnavailableError,
    DependencyUnreachableError,
    TimeoutError,
    ConnectionError,
    httpx.TransportError,  # includes httpx.TimeoutException
)


def default_is_failure(exc: BaseException) -> bool:
    if isinstance(exc, _FAILURE_TYPES):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def default_is_transient(exc: BaseException) -> bool:
    """Retry what trips a breaker, plus 429 Too Many Requests."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return True
    return default_is_failure(exc)
