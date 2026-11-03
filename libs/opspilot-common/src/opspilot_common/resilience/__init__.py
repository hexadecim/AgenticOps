"""Resilience toolkit: circuit breaker, retry, timeout and bulkhead per outbound dependency.

Usage::

    registry = ResilienceRegistry(ResilienceSettings.from_yaml("deploy/resilience.yaml"))
    jira = ResilientHttpClient.from_registry(registry, "jira", base_url=settings.jira_url)
    resp = await jira.post("/rest/api/3/issue", json=body, idempotency_key=approval_id)
"""

from .breaker import CircuitBreaker
from .bulkhead import Bulkhead
from .clock import Clock, MonotonicClock, WallClock
from .config import BreakerConfig, ResilienceConfig, ResilienceSettings, RetryConfig
from .errors import (
    BulkheadFullError,
    CircuitOpenError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    DependencyUnreachableError,
    ResilienceError,
)
from .factory import BREAKER_KEY_PREFIX, redis_registry
from .http import ResilientHttpClient
from .listeners import BreakerListener, LoggingListener, PrometheusListener
from .policy import FailurePolicy, default_is_failure, default_is_transient
from .resilient import ResilienceRegistry, Resilient
from .store import BreakerSnapshot, BreakerStateStore, CircuitState, InMemoryStateStore, RedisStateStore

__all__ = [
    "BREAKER_KEY_PREFIX",
    "BreakerConfig",
    "BreakerListener",
    "BreakerSnapshot",
    "BreakerStateStore",
    "Bulkhead",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "Clock",
    "DependencyTimeoutError",
    "DependencyUnavailableError",
    "DependencyUnreachableError",
    "FailurePolicy",
    "InMemoryStateStore",
    "LoggingListener",
    "MonotonicClock",
    "PrometheusListener",
    "RedisStateStore",
    "ResilienceConfig",
    "ResilienceError",
    "ResilienceRegistry",
    "ResilienceSettings",
    "Resilient",
    "ResilientHttpClient",
    "RetryConfig",
    "WallClock",
    "default_is_failure",
    "default_is_transient",
    "redis_registry",
]
