"""FastAPI routers. They only translate HTTP ⇄ use cases; no business rules here.

No `from __future__ import annotations`: FastAPI must see the locally built `Annotated` aliases.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from opspilot_common.auth import Principal, Role

from ...application.dto import PasswordStepResult, SessionResult, UserView
from ...domain.errors import (
    AccountLockedError,
    ChallengeExpiredError,
    EmailAlreadyRegisteredError,
    IdentityError,
    InvalidCredentialsError,
    InvalidMfaCodeError,
    LastAdminError,
    PasswordChangeRequiredError,
    SelfLockoutError,
    SessionNotRenewableError,
    UserNotFoundError,
    WeakPasswordError,
)
from .schemas import (
    CreateUserRequest,
    Enrolment,
    MfaRequest,
    ResetPasswordRequest,
    SessionOut,
    SignInChallenge,
    SignInRequest,
    UpdateUserRequest,
    UserOut,
)

_STATUS: dict[type[IdentityError], int] = {
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    InvalidMfaCodeError: status.HTTP_401_UNAUTHORIZED,
    ChallengeExpiredError: status.HTTP_401_UNAUTHORIZED,
    SessionNotRenewableError: status.HTTP_401_UNAUTHORIZED,
    AccountLockedError: status.HTTP_423_LOCKED,
    PasswordChangeRequiredError: status.HTTP_428_PRECONDITION_REQUIRED,
    WeakPasswordError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    EmailAlreadyRegisteredError: status.HTTP_409_CONFLICT,
    UserNotFoundError: status.HTTP_404_NOT_FOUND,
    LastAdminError: status.HTTP_409_CONFLICT,
    SelfLockoutError: status.HTTP_409_CONFLICT,
}


def install_error_handlers(app: FastAPI) -> None:
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, IdentityError)
        return JSONResponse(
            {"code": exc.code, "message": exc.message},
            status_code=_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST),
        )

    app.add_exception_handler(IdentityError, handle)


def _user(view: UserView) -> UserOut:
    return UserOut.model_validate(view)


def _session(result: SessionResult) -> SessionOut:
    return SessionOut(token=result.token, expires_in=result.expires_in, user=_user(result.user))


def _challenge(result: PasswordStepResult) -> SignInChallenge:
    enrolment = result.enrolment
    return SignInChallenge(
        challenge_token=result.challenge_token,
        name=result.name,
        mfa_enrolled=enrolment is None,
        enrolment=Enrolment(secret=enrolment.secret, otpauth_uri=enrolment.otpauth_uri)
        if enrolment
        else None,
        password_change_required=result.password_change_required,
    )


def build_router(container: Any) -> APIRouter:
    """`container` is the composition root (see bootstrap.py); typed loosely to keep adapters decoupled."""
    router = APIRouter(prefix="/v1")
    current = container.current_principal
    admin = container.require_admin
    CurrentUser = Annotated[Principal, Depends(current)]
    AdminUser = Annotated[Principal, Depends(admin)]

    @router.post("/sign-in", response_model=SignInChallenge, tags=["auth"])
    async def sign_in(body: SignInRequest) -> SignInChallenge:
        return _challenge(await container.start_sign_in(body.email, body.password))

    @router.post("/sign-in/mfa", response_model=SessionOut, tags=["auth"])
    async def sign_in_mfa(body: MfaRequest) -> SessionOut:
        return _session(await container.complete_sign_in(body.challenge_token, body.code, body.new_password))

    @router.post("/session/renew", response_model=SessionOut, tags=["auth"])
    async def renew(principal: CurrentUser) -> SessionOut:
        return _session(await container.renew_session(principal.user_id))

    @router.post("/sign-out", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
    async def sign_out(principal: CurrentUser, reason: str = "user") -> Response:
        await container.record_sign_out(
            principal.user_id, reason="timeout" if reason == "timeout" else "user"
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/me", response_model=UserOut, tags=["auth"])
    async def me(principal: CurrentUser) -> UserOut:
        return _user(await container.get_current_user(principal.user_id))

    @router.get("/users", response_model=list[UserOut], tags=["users"])
    async def list_users(_: AdminUser) -> list[UserOut]:
        return [_user(u) for u in await container.list_users()]

    @router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, tags=["users"])
    async def create_user(body: CreateUserRequest, actor: AdminUser) -> UserOut:
        return _user(
            await container.create_user(
                actor,
                email=str(body.email),
                name=body.name,
                roles=frozenset(body.roles),
                temporary_password=body.temporary_password,
            )
        )

    @router.patch("/users/{user_id}", response_model=UserOut, tags=["users"])
    async def update_user(user_id: str, body: UpdateUserRequest, actor: AdminUser) -> UserOut:
        return _user(
            await container.update_user(
                actor,
                user_id,
                name=body.name,
                roles=frozenset(body.roles) if body.roles else None,
                active=body.active,
            )
        )

    @router.post("/users/{user_id}/reset-mfa", response_model=UserOut, tags=["users"])
    async def reset_mfa(user_id: str, actor: AdminUser) -> UserOut:
        return _user(await container.reset_mfa(actor, user_id))

    @router.post("/users/{user_id}/reset-password", response_model=UserOut, tags=["users"])
    async def reset_password(user_id: str, body: ResetPasswordRequest, actor: AdminUser) -> UserOut:
        return _user(await container.reset_password(actor, user_id, body.temporary_password))

    @router.get("/roles", tags=["users"])
    async def roles(_: CurrentUser) -> list[dict[str, str]]:
        from opspilot_common.auth import ROLE_LABELS

        return [{"id": r.value, "label": ROLE_LABELS[r]} for r in Role]

    return router
