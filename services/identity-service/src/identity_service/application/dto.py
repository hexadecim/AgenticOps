"""Results returned by use cases; adapters translate them to HTTP shapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.user import User


@dataclass(frozen=True, slots=True)
class MfaEnrolment:
    secret: str
    otpauth_uri: str


@dataclass(frozen=True, slots=True)
class PasswordStepResult:
    challenge_token: str
    name: str
    enrolment: MfaEnrolment | None
    password_change_required: bool


@dataclass(frozen=True, slots=True)
class UserView:
    id: str
    email: str
    name: str
    roles: list[str]
    active: bool
    mfa_enrolled: bool
    password_change_required: bool
    locked: bool
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def of(cls, user: User, now: datetime) -> UserView:
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            roles=sorted(r.value for r in user.roles),
            active=user.active,
            mfa_enrolled=user.mfa_enrolled,
            password_change_required=user.password_change_required,
            locked=user.is_locked(now),
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


@dataclass(frozen=True, slots=True)
class SessionResult:
    token: str
    expires_in: int
    user: UserView
