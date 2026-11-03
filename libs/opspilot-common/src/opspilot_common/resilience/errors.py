"""Errors raised by the resilience layer. Callers map these to fallbacks."""


class ResilienceError(Exception):
    """Base class for errors produced by the resilience layer itself."""

    def __init__(self, dependency: str, message: str) -> None:
        super().__init__(f"[{dependency}] {message}")
        self.dependency = dependency


class CircuitOpenError(ResilienceError):
    """The circuit for a dependency is open; the call was not attempted."""

    def __init__(self, dependency: str, retry_after: float) -> None:
        super().__init__(dependency, f"circuit open, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class BulkheadFullError(ResilienceError):
    """Too many concurrent calls to a dependency; the call was rejected."""

    def __init__(self, dependency: str) -> None:
        super().__init__(dependency, "bulkhead full")


class DependencyTimeoutError(ResilienceError):
    """A single attempt exceeded its timeout."""

    def __init__(self, dependency: str, timeout: float) -> None:
        super().__init__(dependency, f"timed out after {timeout:.1f}s")
        self.timeout = timeout


class DependencyUnavailableError(ResilienceError):
    """The dependency answered with a server error (5xx) after all retries."""

    def __init__(self, dependency: str, status_code: int) -> None:
        super().__init__(dependency, f"server error {status_code}")
        self.status_code = status_code


class DependencyUnreachableError(ResilienceError):
    """The dependency could not be reached (connection refused/reset, DNS, TLS)."""

    def __init__(self, dependency: str, reason: str) -> None:
        super().__init__(dependency, f"unreachable: {reason}")
