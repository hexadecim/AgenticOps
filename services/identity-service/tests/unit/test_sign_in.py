from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from identity_service.adapters.outbound.memory import InMemoryUnitOfWork, InMemoryUserRepository
from identity_service.adapters.outbound.security import (
    Argon2PasswordHasher,
    FernetSecretCipher,
    JwtChallengeTokens,
    JwtSessionIssuer,
    PyOtpTotpProvider,
)
from identity_service.application.auth import CompleteSignIn, RenewSession, StartSignIn
from identity_service.application.users import CreateUser, UpdateUser
from identity_service.domain.errors import (
    AccountLockedError,
    ChallengeExpiredError,
    InvalidCredentialsError,
    InvalidMfaCodeError,
    LastAdminError,
    PasswordChangeRequiredError,
    SelfLockoutError,
    SessionNotRenewableError,
    WeakPasswordError,
)
from identity_service.domain.policies import LockoutPolicy, PasswordPolicy
from opspilot_common.auth import AuthKeys, Principal, Role, TokenVerifier

from ..conftest import FakeClock, totp_code

SECRET = "unit-test-jwt-secret-at-least-32-characters"
ADMIN = Principal("admin-1", "Admin", frozenset({Role.ADMIN}))


class World:
    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.store, self.events = clock, InMemoryUserRepository(), []
        uow = lambda: InMemoryUnitOfWork(self.store, self.events)  # noqa: E731
        hasher, totp = Argon2PasswordHasher(), PyOtpTotpProvider()
        cipher, challenges = FernetSecretCipher(Fernet.generate_key().decode()), JwtChallengeTokens(SECRET)
        sessions = JwtSessionIssuer(AuthKeys.load("identity-service", SECRET), timedelta(minutes=30))
        self.start = StartSignIn(uow, hasher, totp, cipher, challenges, clock, LockoutPolicy())
        self.complete = CompleteSignIn(
            uow, hasher, totp, cipher, challenges, sessions, clock, LockoutPolicy(), PasswordPolicy()
        )
        self.renew = RenewSession(uow, sessions, clock)
        self.create = CreateUser(uow, hasher, PasswordPolicy(), clock)
        self.update = UpdateUser(uow, clock)

    def actions(self) -> list[str]:
        return [p.action for p, _ in self.events]  # type: ignore[attr-defined]


@pytest.fixture
def world(clock: FakeClock) -> World:
    return World(clock)


async def first_sign_in(world: World, email: str = "lena@example.eu", temp: str = "Temporary-pass-1") -> str:
    """Create a user and complete first sign-in (enrol TOTP + change password). Returns the TOTP secret."""
    await world.create(
        ADMIN, email=email, name="Lena", roles=frozenset({Role.L1_ANALYST}), temporary_password=temp
    )
    step = await world.start(email, temp)
    assert step.enrolment is not None and step.password_change_required
    await world.complete(
        step.challenge_token,
        totp_code(step.enrolment.secret, world.clock),
        new_password="A-much-better-passphrase",
    )
    return step.enrolment.secret


async def test_first_sign_in_enrols_mfa_changes_password_and_issues_session(world: World) -> None:
    await world.create(
        ADMIN,
        email="Lena@Example.eu ",
        name="Lena",
        roles=frozenset({Role.L1_ANALYST}),
        temporary_password="Temporary-pass-1",
    )
    step = await world.start("lena@example.eu", "Temporary-pass-1")
    assert step.enrolment and step.enrolment.otpauth_uri.startswith(
        "otpauth://totp/OpsPilot:lena%40example.eu"
    )
    with pytest.raises(PasswordChangeRequiredError):
        await world.complete(step.challenge_token, totp_code(step.enrolment.secret, world.clock))
    result = await world.complete(
        step.challenge_token,
        totp_code(step.enrolment.secret, world.clock),
        new_password="A-much-better-passphrase",
    )
    principal = TokenVerifier(SECRET).verify(result.token)
    assert principal.roles == {Role.L1_ANALYST} and principal.mfa
    assert result.user.mfa_enrolled and not result.user.password_change_required
    assert world.actions() == ["user_created", "mfa_enrolled", "password_changed", "signed_in"]


async def test_second_sign_in_needs_no_enrolment(world: World) -> None:
    secret = await first_sign_in(world)
    world.clock.advance(seconds=60)
    step = await world.start("lena@example.eu", "A-much-better-passphrase")
    assert step.enrolment is None and not step.password_change_required
    await world.complete(step.challenge_token, totp_code(secret, world.clock))


async def test_totp_code_cannot_be_replayed(world: World) -> None:
    secret = await first_sign_in(world)
    code = totp_code(secret, world.clock)
    step = await world.start("lena@example.eu", "A-much-better-passphrase")
    with pytest.raises(InvalidMfaCodeError):
        await world.complete(step.challenge_token, code)  # same 30 s window as enrolment


async def test_unknown_email_and_wrong_password_look_identical(world: World) -> None:
    await first_sign_in(world)
    with pytest.raises(InvalidCredentialsError) as unknown:
        await world.start("nobody@example.eu", "whatever-password")
    with pytest.raises(InvalidCredentialsError) as wrong:
        await world.start("lena@example.eu", "wrong-password-123")
    assert unknown.value.message == wrong.value.message


async def test_lockout_after_five_failures_then_expires(world: World) -> None:
    await first_sign_in(world)
    for _ in range(4):
        with pytest.raises(InvalidCredentialsError):
            await world.start("lena@example.eu", "wrong-password-123")
    with pytest.raises(AccountLockedError):
        await world.start("lena@example.eu", "wrong-password-123")
    with pytest.raises(AccountLockedError):  # even the right password is refused while locked
        await world.start("lena@example.eu", "A-much-better-passphrase")
    world.clock.advance(minutes=16)
    assert await world.start("lena@example.eu", "A-much-better-passphrase")
    assert "account_locked" in world.actions()


async def test_wrong_mfa_codes_count_towards_lockout(world: World) -> None:
    await first_sign_in(world)
    step = await world.start("lena@example.eu", "A-much-better-passphrase")
    for _ in range(4):
        with pytest.raises(InvalidMfaCodeError):
            await world.complete(step.challenge_token, "000000")
    with pytest.raises(AccountLockedError):
        await world.complete(step.challenge_token, "000000")


async def test_weak_new_password_does_not_burn_the_code(world: World) -> None:
    await world.create(
        ADMIN,
        email="sam@example.eu",
        name="Sam",
        roles=frozenset({Role.SUPPORT_AGENT}),
        temporary_password="Temporary-pass-1",
    )
    step = await world.start("sam@example.eu", "Temporary-pass-1")
    assert step.enrolment
    code = totp_code(step.enrolment.secret, world.clock)
    with pytest.raises(WeakPasswordError):
        await world.complete(step.challenge_token, code, new_password="short")
    await world.complete(step.challenge_token, code, new_password="A-much-better-passphrase")


async def test_challenge_token_is_not_a_session_token(world: World) -> None:
    await first_sign_in(world)
    step = await world.start("lena@example.eu", "A-much-better-passphrase")
    with pytest.raises(Exception):  # noqa: B017 - any verification failure
        TokenVerifier(SECRET).verify(step.challenge_token)
    with pytest.raises(ChallengeExpiredError):
        await world.complete("not-a-token", "123456")


async def test_renewal_refused_after_deactivation(world: World) -> None:
    await first_sign_in(world)
    user = next(iter(world.store.rows.values()))
    await world.renew(user.id)
    await world.update(ADMIN, user.id, active=False)
    with pytest.raises(SessionNotRenewableError):
        await world.renew(user.id)


async def test_last_admin_and_self_lockout_are_protected(world: World) -> None:
    admin_view = await world.create(
        ADMIN,
        email="root@example.eu",
        name="Root",
        roles=frozenset({Role.ADMIN}),
        temporary_password="Temporary-pass-1",
    )
    me = Principal(admin_view.id, "Root", frozenset({Role.ADMIN}))
    with pytest.raises(SelfLockoutError):
        await world.update(me, admin_view.id, active=False)
    with pytest.raises(LastAdminError):
        await world.update(ADMIN, admin_view.id, roles=frozenset({Role.L1_ANALYST}))
    second = await world.create(
        ADMIN,
        email="root2@example.eu",
        name="Root 2",
        roles=frozenset({Role.ADMIN}),
        temporary_password="Temporary-pass-1",
    )
    await world.update(ADMIN, second.id, roles=frozenset({Role.SUPPORT_MANAGER}))
