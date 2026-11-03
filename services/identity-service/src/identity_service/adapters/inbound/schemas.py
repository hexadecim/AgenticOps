"""HTTP request/response models (the service's public contract with api-gateway)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from opspilot_common.auth import Role
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


def _check_email(value: str) -> str:
    # Deliberately permissive: internal domains such as `.local` are valid work addresses here.
    value = value.strip()
    if len(value) > 320 or not _EMAIL.match(value):
        raise ValueError("Enter a valid email address, like name@company.com")
    return value


Email = Annotated[str, AfterValidator(_check_email)]


class SignInRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class Enrolment(BaseModel):
    secret: str
    otpauth_uri: str


class SignInChallenge(BaseModel):
    challenge_token: str
    name: str
    mfa_enrolled: bool
    enrolment: Enrolment | None
    password_change_required: bool


class MfaRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str | None = Field(default=None, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    roles: list[Role]
    active: bool
    mfa_enrolled: bool
    password_change_required: bool
    locked: bool
    created_at: datetime
    last_login_at: datetime | None


class SessionOut(BaseModel):
    token: str
    expires_in: int
    user: UserOut


class CreateUserRequest(BaseModel):
    email: Email
    name: str = Field(min_length=1, max_length=200)
    roles: list[Role] = Field(min_length=1)
    temporary_password: str = Field(min_length=1, max_length=256)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    roles: list[Role] | None = Field(default=None, min_length=1)
    active: bool | None = None


class ResetPasswordRequest(BaseModel):
    temporary_password: str = Field(min_length=1, max_length=256)


class ErrorOut(BaseModel):
    code: str
    message: str
