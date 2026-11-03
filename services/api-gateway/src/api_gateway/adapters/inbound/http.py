"""Browser-facing routes under /api. The session JWT lives only in an HttpOnly cookie.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from opspilot_common.auth import InvalidTokenError, Principal, TokenVerifier
from opspilot_common.resilience import CircuitOpenError, ResilienceError
from opspilot_common.web import SESSION_COOKIE
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ...application import SignIn, SystemHealth, TooManyAttemptsError
from ...ports import (
    ApprovalPort,
    AuditPort,
    DownstreamError,
    IdentityPort,
    MailPort,
    OpsPort,
    PrivacyPort,
    ReportingPort,
    RunsPort,
    SettingsPort,
)
from .live import Broadcaster

CSRF_HEADER = "X-OpsPilot-Client"
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class SignInBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class MfaBody(BaseModel):
    challenge_token: str = Field(max_length=2048)
    code: str = Field(max_length=6)
    new_password: str | None = Field(default=None, max_length=256)


class CsrfMiddleware(BaseHTTPMiddleware):
    """State-changing API calls must carry a custom header. Browsers cannot add it cross-site without a CORS
    preflight, which the gateway does not grant to foreign origins; together with SameSite=Strict cookies
    this blocks CSRF."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            request.method in _UNSAFE
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != "web"
        ):
            return JSONResponse({"code": "csrf", "message": "Missing client header."}, status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def install_error_handlers(app: FastAPI) -> None:
    async def downstream(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DownstreamError)
        response = JSONResponse(exc.body, status_code=exc.status_code)
        if exc.status_code == 401 and request.url.path.startswith(("/api/auth/renew", "/api/auth/me")):
            response.delete_cookie(SESSION_COOKIE, path="/api")
        return response

    async def unavailable(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ResilienceError)
        retry = int(exc.retry_after) + 1 if isinstance(exc, CircuitOpenError) else 30
        return JSONResponse(
            {
                "code": "service_unavailable",
                "dependency": exc.dependency,
                "message": "This part of OpsPilot is temporarily unavailable. Try again in a minute.",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": str(retry)},
        )

    async def throttled(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"code": "too_many_attempts", "message": "Too many attempts. Wait a minute."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "60"},
        )

    app.add_exception_handler(DownstreamError, downstream)
    app.add_exception_handler(ResilienceError, unavailable)
    app.add_exception_handler(TooManyAttemptsError, throttled)


def _set_session(response: Response, token: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=secure, samesite="strict", path="/api"
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def build_router(
    identity: IdentityPort,
    sign_in: SignIn,
    settings: SettingsPort,
    mail: MailPort,
    audit: AuditPort,
    privacy: PrivacyPort,
    approvals: ApprovalPort,
    runs: RunsPort,
    reporting: ReportingPort,
    ops: OpsPort,
    live: Broadcaster,
    verifier: TokenVerifier,
    health: SystemHealth,
    require_admin: Any,
    *,
    cookie_secure: bool,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def session_token(opspilot_session: Annotated[str | None, Cookie()] = None) -> str:
        if not opspilot_session:
            raise DownstreamError(401, {"code": "not_signed_in", "message": "Sign in to continue."})
        return opspilot_session

    Token = Annotated[str, Depends(session_token)]

    # --- authentication -------------------------------------------------------------------------------
    @router.post("/auth/sign-in", tags=["auth"])
    async def password_step(body: SignInBody, request: Request) -> dict[str, Any]:
        return await sign_in.password_step(_client(request), body.email, body.password)

    @router.post("/auth/sign-in/mfa", tags=["auth"])
    async def mfa_step(body: MfaBody, request: Request, response: Response) -> dict[str, Any]:
        result = await sign_in.mfa_step(_client(request), body.challenge_token, body.code, body.new_password)
        _set_session(response, result["token"], result["expires_in"], cookie_secure)
        return {"user": result["user"], "expires_in": result["expires_in"]}

    @router.post("/auth/renew", tags=["auth"])
    async def renew(token: Token, response: Response) -> dict[str, Any]:
        result = await identity.renew(token)
        _set_session(response, result["token"], result["expires_in"], cookie_secure)
        return {"user": result["user"], "expires_in": result["expires_in"]}

    @router.post("/auth/sign-out", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
    async def sign_out(
        response: Response, reason: str = "user", opspilot_session: Annotated[str | None, Cookie()] = None
    ) -> Response:
        if opspilot_session:
            # Signing out must always succeed for the browser, even when identity-service is down.
            with contextlib.suppress(DownstreamError, ResilienceError):
                await identity.sign_out(opspilot_session, "timeout" if reason == "timeout" else "user")
        out = Response(status_code=status.HTTP_204_NO_CONTENT)
        out.delete_cookie(SESSION_COOKIE, path="/api")
        return out

    @router.get("/auth/me", tags=["auth"])
    async def me(token: Token) -> dict[str, Any]:
        return await identity.me(token)

    @router.get("/roles", tags=["admin"])
    async def roles(token: Token) -> list[dict[str, Any]]:
        return await identity.roles(token)

    # --- user administration (identity-service enforces the Admin role) -----------------------------------
    @router.get("/admin/users", tags=["admin"])
    async def list_users(token: Token) -> list[dict[str, Any]]:
        return await identity.list_users(token)

    @router.post("/admin/users", status_code=status.HTTP_201_CREATED, tags=["admin"])
    async def create_user(token: Token, body: dict[str, Any]) -> dict[str, Any]:
        return await identity.create_user(token, body)

    @router.patch("/admin/users/{user_id}", tags=["admin"])
    async def update_user(token: Token, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await identity.update_user(token, user_id, body)

    @router.post("/admin/users/{user_id}/reset-mfa", tags=["admin"])
    async def reset_mfa(token: Token, user_id: str) -> dict[str, Any]:
        return await identity.reset_mfa(token, user_id)

    @router.post("/admin/users/{user_id}/reset-password", tags=["admin"])
    async def reset_password(token: Token, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await identity.reset_password(token, user_id, body)

    # --- integrations (settings-service enforces the Admin role) ----------------------------------------
    @router.get("/admin/settings", tags=["admin"])
    async def list_sections(token: Token) -> list[dict[str, Any]]:
        return await settings.list_sections(token)

    @router.get("/admin/settings/{section}", tags=["admin"])
    async def get_section(token: Token, section: str) -> dict[str, Any]:
        return await settings.get_section(token, section)

    @router.put("/admin/settings/{section}", tags=["admin"])
    async def save_section(token: Token, section: str, body: dict[str, Any]) -> dict[str, Any]:
        return await settings.save_section(token, section, body)

    @router.post("/admin/settings/{section}/test", tags=["admin"])
    async def test_section(token: Token, section: str, body: dict[str, Any]) -> dict[str, Any]:
        return await settings.test_section(token, section, body)

    # --- mailbox (mail-service checks the session; "Check now" is Admin only) -----------------------------
    @router.get("/mail/status", tags=["mail"])
    async def mail_status(token: Token) -> dict[str, Any]:
        return await mail.status(token)

    @router.post("/admin/mail/poll", tags=["admin"])
    async def mail_poll(token: Token) -> dict[str, Any]:
        return await mail.poll(token)

    # --- audit log (audit-service checks the reader role) -------------------------------------------------
    @router.get("/audit/entries", tags=["audit"])
    async def audit_entries(
        token: Token,
        kind: str | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        params = {k: str(v) for k, v in {"kind": kind, "q": q, "cursor": cursor, "limit": limit}.items() if v}
        return await audit.entries(token, params)

    # --- identity reveal (privacy-service checks the role and records the access) ------------------------
    @router.post("/privacy/reveals", tags=["privacy"])
    async def reveal_identity(token: Token, body: dict[str, Any]) -> dict[str, Any]:
        return await privacy.reveal(token, body)

    # --- approvals and runs (the services check roles per gate) ----------------------------------------
    @router.get("/approvals", tags=["approvals"])
    async def approval_queue(token: Token, decided: bool = False, gate: str | None = None) -> dict[str, Any]:
        params = {"decided": str(decided).lower(), **({"gate": gate} if gate else {})}
        return await approvals.queue(token, params)

    @router.get("/approvals/{approval_id}", tags=["approvals"])
    async def approval(token: Token, approval_id: str) -> dict[str, Any]:
        return await approvals.get(token, approval_id)

    @router.post("/approvals/{approval_id}/decision", tags=["approvals"])
    async def decide(token: Token, approval_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await approvals.decide(token, approval_id, body)

    @router.get("/fixes/{approval_id}/evidence", tags=["approvals"])
    async def fix_evidence(token: Token, approval_id: str) -> dict[str, Any]:
        """Fresh probes and masked logs for a pending T2 fix. 503 when ops-service's circuit is open: the
        console then shows "Evidence unavailable" and disables Approve."""
        return await ops.evidence(token, approval_id)

    @router.get("/runs/summary", tags=["approvals"])
    async def run_summary(token: Token) -> dict[str, Any]:
        return await runs.summary(token)

    # --- dashboard (reporting-service read models) ------------------------------------------------------
    @router.get("/dashboard", tags=["dashboard"])
    async def dashboard(token: Token) -> dict[str, Any]:
        return await reporting.dashboard(token)

    @router.get("/incidents/{key}/timeline", tags=["dashboard"])
    async def incident_timeline(token: Token, key: str) -> dict[str, Any]:
        return await reporting.timeline(token, key)

    @router.get("/events", tags=["approvals"], response_model=None)
    async def events(token: Token) -> StreamingResponse:
        """Server-sent events: type and ids of approval, run and Jira events. Signed-in users only."""
        try:
            verifier.verify(token)
        except InvalidTokenError as exc:
            raise DownstreamError(401, {"code": "not_signed_in", "message": "Sign in to continue."}) from exc
        return StreamingResponse(
            live.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- system health (owned by the gateway; Admin role checked here) -----------------------------------
    @router.get("/admin/system/circuits", tags=["admin"])
    async def circuits(_: Annotated[Principal, Depends(require_admin)]) -> list[dict[str, Any]]:
        return await health.circuits()

    return router
