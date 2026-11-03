"""User aggregate. All state changes go through methods so invariants live in one place."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role

from .policies import LockoutPolicy


def normalise_email(email: str) -> str:
    return email.strip().lower()


@dataclass(slots=True)
class User:
    id: str
    email: str
    name: str
    password_hash: str
    roles: frozenset[Role]
    created_at: datetime
    active: bool = True
    password_change_required: bool = False
    mfa_secret_enc: str | None = None
    mfa_enrolled: bool = False
    mfa_last_step: int = 0
    failed_logins: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    updated_at: datetime | None = field(default=None)

    @classmethod
    def register(
        cls,
        *,
        email: str,
        name: str,
        password_hash: str,
        roles: frozenset[Role],
        now: datetime,
        password_change_required: bool = True,
    ) -> User:
        return cls(
            id=str(uuid.uuid4()),
            email=normalise_email(email),
            name=name.strip(),
            password_hash=password_hash,
            roles=roles,
            created_at=now,
            updated_at=now,
            password_change_required=password_change_required,
        )

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until

    def record_failed_attempt(self, now: datetime, policy: LockoutPolicy) -> None:
        self.failed_logins += 1
        if self.failed_logins >= policy.max_attempts:
            self.locked_until = now + policy.lock_for
            self.failed_logins = 0
        self.updated_at = now

    def clear_failed_attempts(self, now: datetime) -> None:
        if self.failed_logins or self.locked_until:
            self.failed_logins, self.locked_until, self.updated_at = 0, None, now

    def begin_mfa_enrolment(self, secret_enc: str, now: datetime) -> None:
        self.mfa_secret_enc, self.mfa_enrolled, self.mfa_last_step, self.updated_at = (
            secret_enc,
            False,
            0,
            now,
        )

    def accept_mfa_step(self, step: int, now: datetime) -> bool:
        """Accept a verified TOTP time step once; replaying the same or an older code is refused."""
        if step <= self.mfa_last_step:
            return False
        self.mfa_last_step, self.mfa_enrolled, self.updated_at = step, True, now
        return True

    def complete_sign_in(self, now: datetime) -> None:
        self.failed_logins, self.locked_until, self.last_login_at, self.updated_at = 0, None, now, now

    def change_password(self, password_hash: str, now: datetime, *, temporary: bool = False) -> None:
        self.password_hash, self.password_change_required, self.updated_at = password_hash, temporary, now

    def reset_mfa(self, now: datetime) -> None:
        self.mfa_secret_enc, self.mfa_enrolled, self.mfa_last_step, self.updated_at = None, False, 0, now

    def update_profile(
        self,
        now: datetime,
        *,
        name: str | None = None,
        roles: frozenset[Role] | None = None,
        active: bool | None = None,
    ) -> list[str]:
        changed: list[str] = []
        if name is not None and name.strip() != self.name:
            self.name, changed = name.strip(), [*changed, "name"]
        if roles is not None and roles != self.roles:
            self.roles, changed = roles, [*changed, "roles"]
        if active is not None and active != self.active:
            self.active, changed = active, [*changed, "active"]
        if changed:
            self.updated_at = now
        return changed

    def principal(self) -> Principal:
        return Principal(user_id=self.id, name=self.name, roles=self.roles, mfa=self.mfa_enrolled)
