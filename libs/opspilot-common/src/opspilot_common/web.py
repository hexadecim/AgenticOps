"""FastAPI helpers (optional extra `opspilot-common[fastapi]`): auth dependencies, correlation ids, health."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import InvalidTokenError, Principal, Role, ServiceTokenVerifier, TokenVerifier
from .logging import correlation_id, new_correlation_id
from .resilience import CircuitState, ResilienceRegistry

CORRELATION_HEADER = "X-Correlation-ID"
SESSION_COOKIE = "opspilot_session"

_bearer = HTTPBearer(auto_error=False)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = correlation_id.set(request.headers.get(CORRELATION_HEADER) or new_correlation_id())
        try:
            response = await call_next(request)
            response.headers[CORRELATION_HEADER] = correlation_id.get() or ""
            return response
        finally:
            correlation_id.reset(token)


def principal_dependency(verifier: TokenVerifier) -> Callable[..., Awaitable[Principal]]:
    """Reads the session from the Bearer header or the session cookie."""

    async def current_principal(
        request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
    ) -> Principal:
        token = creds.credentials if creds else request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
        try:
            return verifier.verify(token)
        except InvalidTokenError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or invalid") from exc

    return current_principal


def require_roles(
    current: Callable[..., Awaitable[Principal]], *roles: Role
) -> Callable[..., Awaitable[Principal]]:
    async def checker(principal: Principal = Depends(current)) -> Principal:
        if not principal.has_any(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot perform this action")
        return principal

    return checker


def service_dependency(verifier: ServiceTokenVerifier, *allowed: str) -> Callable[..., Awaitable[str]]:
    """For `/internal/...` routes: accepts only service tokens from the named services; returns the caller."""

    async def calling_service(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
        if creds is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Service token required")
        try:
            return verifier.verify(creds.credentials, allowed or None)
        except InvalidTokenError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This service may not call this API") from exc

    return calling_service


def health_routes(registry: ResilienceRegistry, critical: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    """`/health/live`, `/health/ready` (503 when a *critical* dependency's circuit is open) and `/metrics`."""
    from fastapi import APIRouter

    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(response: Response) -> dict[str, object]:
        states = await registry.states()
        down = sorted(n for n, s in states.items() if s is CircuitState.OPEN and n in critical)
        if down:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded" if down else "ok", "circuits": {n: s.value for n, s in states.items()}}

    @router.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router
