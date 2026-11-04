"""httpx client whose every request runs under a dependency's resilience policies."""

from __future__ import annotations

from typing import Any

import httpx

from ..telemetry import inject_traceparent
from .errors import DependencyTimeoutError, DependencyUnavailableError, DependencyUnreachableError
from .resilient import ResilienceRegistry, Resilient

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# Our own services join the caller's trace. Third parties (Jira, Google, Microsoft) never see trace ids.
_INTERNAL = frozenset(
    {
        "agent-orchestrator",
        "approval-service",
        "audit-service",
        "identity-service",
        "jira-connector",
        "mail-service",
        "ops-service",
        "privacy-service",
        "reporting-service",
        "settings-service",
    }
)


class ResilientHttpClient:
    """Thin adapter over `httpx.AsyncClient`.

    - 5xx responses raise `DependencyUnavailableError` (counted by the breaker, retried).
    - 4xx responses are returned to the caller unchanged (never trip the breaker).
    - Non-idempotent methods are retried only when an `idempotency_key` is supplied.
    - Transport failures surface as `ResilienceError` subclasses, so callers handle one error family.
    """

    def __init__(self, resilient: Resilient, client: httpx.AsyncClient) -> None:
        self._resilient = resilient
        self._client = client

    @classmethod
    def from_registry(
        cls, registry: ResilienceRegistry, name: str, *, base_url: str = "", **client_kwargs: Any
    ) -> ResilientHttpClient:
        resilient = registry.get(name)
        client = httpx.AsyncClient(base_url=base_url, timeout=resilient.config.timeout, **client_kwargs)
        return cls(resilient, client)

    @property
    def dependency(self) -> str:
        return self._resilient.name

    async def request(
        self, method: str, url: str, *, idempotency_key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        method = method.upper()
        if idempotency_key:
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "Idempotency-Key": idempotency_key}
        retryable = method in _IDEMPOTENT_METHODS or idempotency_key is not None

        async def attempt() -> httpx.Response:
            name = self._resilient.name
            if name in _INTERNAL:  # per attempt: the span of this call is current here
                inject_traceparent(kwargs.setdefault("headers", {}))
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                raise DependencyTimeoutError(name, self._resilient.config.timeout) from exc
            except httpx.TransportError as exc:
                raise DependencyUnreachableError(name, type(exc).__name__) from exc
            if response.status_code >= 500:
                raise DependencyUnavailableError(name, response.status_code)
            return response

        return await self._resilient.call(attempt, retry=retryable)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
