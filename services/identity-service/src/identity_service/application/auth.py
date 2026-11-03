"""Sign-in use cases: password step → TOTP step (with first-time enrolment) → session; renew; sign out."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import NoReturn

from opspilot_common.events.catalogue import UserActivity

from ..domain.errors import (
    AccountLockedError,
    ChallengeExpiredError,
    InvalidCredentialsError,
    InvalidMfaCodeError,
    PasswordChangeRequiredError,
    SessionNotRenewableError,
)
from ..domain.policies import LockoutPolicy, PasswordPolicy
from ..domain.user import User, normalise_email
from ..ports.repositories import UnitOfWork
from ..ports.security import (
    ChallengeTokens,
    Clock,
    PasswordHasher,
    SecretCipher,
    SessionTokenIssuer,
    TotpProvider,
)
from .dto import MfaEnrolment, PasswordStepResult, SessionResult, UserView

UowFactory = Callable[[], UnitOfWork]


class StartSignIn:
    """Step 1: verify the password; hand back a short-lived challenge and, if needed, a new TOTP secret."""

    def __init__(
        self,
        uow: UowFactory,
        hasher: PasswordHasher,
        totp: TotpProvider,
        cipher: SecretCipher,
        challenges: ChallengeTokens,
        clock: Clock,
        lockout: LockoutPolicy,
    ) -> None:
        self._uow, self._hasher, self._totp, self._cipher = uow, hasher, totp, cipher
        self._challenges, self._clock, self._lockout = challenges, clock, lockout
        self._dummy_hash = hasher.hash("timing-equaliser-not-a-real-password")

    async def __call__(self, email: str, password: str) -> PasswordStepResult:
        now = self._clock.now()
        async with self._uow() as uow:
            user = await uow.users.get_by_email(normalise_email(email))
            if user is None:
                self._hasher.verify(self._dummy_hash, password)  # same cost as a real check
                raise InvalidCredentialsError()
            if user.is_locked(now):
                raise AccountLockedError(_locked_until(user))
            if not self._hasher.verify(user.password_hash, password) or not user.active:
                await _fail(uow, user, now, self._lockout, "password")
            if self._hasher.needs_rehash(user.password_hash):
                user.change_password(
                    self._hasher.hash(password), now, temporary=user.password_change_required
                )
            enrolment = None
            if not user.mfa_enrolled:
                secret = self._totp.new_secret()
                user.begin_mfa_enrolment(self._cipher.encrypt(secret), now)
                enrolment = MfaEnrolment(secret, self._totp.provisioning_uri(secret, user.email))
            await uow.users.save(user)
            await uow.commit()
            return PasswordStepResult(
                challenge_token=self._challenges.issue(user.id),
                name=user.name,
                enrolment=enrolment,
                password_change_required=user.password_change_required,
            )


class CompleteSignIn:
    """Step 2: verify the TOTP code (enrolling on first use), set a required new password, open a session."""

    def __init__(
        self,
        uow: UowFactory,
        hasher: PasswordHasher,
        totp: TotpProvider,
        cipher: SecretCipher,
        challenges: ChallengeTokens,
        sessions: SessionTokenIssuer,
        clock: Clock,
        lockout: LockoutPolicy,
        password_policy: PasswordPolicy,
    ) -> None:
        self._uow, self._hasher, self._totp, self._cipher = uow, hasher, totp, cipher
        self._challenges, self._sessions, self._clock = challenges, sessions, clock
        self._lockout, self._password_policy = lockout, password_policy

    async def __call__(
        self, challenge_token: str, code: str, new_password: str | None = None
    ) -> SessionResult:
        user_id = self._challenges.verify(challenge_token)
        now = self._clock.now()
        async with self._uow() as uow:
            user = await uow.users.get(user_id)
            if user is None or not user.active:
                raise InvalidCredentialsError()
            if user.is_locked(now):
                raise AccountLockedError(_locked_until(user))
            if user.mfa_secret_enc is None:
                raise ChallengeExpiredError()
            # Validate the password change before consuming the one-time code, so a weak password
            # does not burn the code and force the person to wait for the next one.
            if user.password_change_required:
                if not new_password:
                    raise PasswordChangeRequiredError()
                self._password_policy.validate(new_password, email=user.email)
            step = self._totp.verify(self._cipher.decrypt(user.mfa_secret_enc), code.strip(), now)
            was_enrolled = user.mfa_enrolled
            if step is None or not user.accept_mfa_step(step, now):
                await _fail(uow, user, now, self._lockout, "mfa", error=InvalidMfaCodeError())
            if not was_enrolled:
                uow.events.record(
                    UserActivity(user_id=user.id, action="mfa_enrolled", actor_role=_role(user)),
                    actor=user.id,
                )
            if user.password_change_required and new_password:
                user.change_password(self._hasher.hash(new_password), now)
                uow.events.record(
                    UserActivity(user_id=user.id, action="password_changed", actor_role=_role(user)),
                    actor=user.id,
                )
            user.complete_sign_in(now)
            await uow.users.save(user)
            uow.events.record(
                UserActivity(user_id=user.id, action="signed_in", actor_role=_role(user)), actor=user.id
            )
            await uow.commit()
            return SessionResult(
                self._sessions.issue(user.principal()), self._sessions.ttl_seconds, UserView.of(user, now)
            )


class RenewSession:
    """Sliding 30-minute session: renewal re-reads the user, so role changes and deactivation apply."""

    def __init__(self, uow: UowFactory, sessions: SessionTokenIssuer, clock: Clock) -> None:
        self._uow, self._sessions, self._clock = uow, sessions, clock

    async def __call__(self, user_id: str) -> SessionResult:
        now = self._clock.now()
        async with self._uow() as uow:
            user = await uow.users.get(user_id)
            if user is None or not user.active or not user.mfa_enrolled or user.is_locked(now):
                raise SessionNotRenewableError()
            return SessionResult(
                self._sessions.issue(user.principal()), self._sessions.ttl_seconds, UserView.of(user, now)
            )


class GetCurrentUser:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, user_id: str) -> UserView:
        async with self._uow() as uow:
            user = await uow.users.get(user_id)
            if user is None or not user.active:
                raise SessionNotRenewableError()
            return UserView.of(user, self._clock.now())


class RecordSignOut:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, user_id: str, *, reason: str = "user") -> None:
        async with self._uow() as uow:
            user = await uow.users.get(user_id)
            uow.events.record(
                UserActivity(
                    user_id=user_id,
                    action="signed_out",
                    detail=reason,
                    actor_role=_role(user) if user else None,
                ),
                actor=user_id,
            )
            await uow.commit()


async def _fail(
    uow: UnitOfWork,
    user: User,
    now: datetime,
    lockout: LockoutPolicy,
    step: str,
    error: Exception | None = None,
) -> NoReturn:
    """Persist the failed attempt (and possible lock) before raising."""
    user.record_failed_attempt(now, lockout)
    await uow.users.save(user)
    locked = user.is_locked(now)
    uow.events.record(
        UserActivity(
            user_id=user.id,
            action="account_locked" if locked else "sign_in_failed",
            detail=step,
            actor_role=_role(user),
        ),
        actor=user.id,
    )
    await uow.commit()
    if locked:
        raise AccountLockedError(_locked_until(user))
    raise error or InvalidCredentialsError()


def _locked_until(user: User) -> datetime:
    assert user.locked_until is not None
    return user.locked_until


def _role(user: User) -> str | None:
    """Role recorded with the user's own actions (audit shows roles, never names)."""
    role = user.principal().primary_role
    return role.value if role else None
