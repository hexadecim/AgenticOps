"""User administration use cases (Admin role). Every change is recorded as an event for the audit log."""

from __future__ import annotations

from collections.abc import Callable

from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.events.catalogue import UserActivity

from ..domain.errors import EmailAlreadyRegisteredError, LastAdminError, SelfLockoutError, UserNotFoundError
from ..domain.policies import PasswordPolicy
from ..domain.user import User, normalise_email
from ..ports.repositories import UnitOfWork
from ..ports.security import Clock, PasswordHasher
from .dto import UserView

UowFactory = Callable[[], UnitOfWork]


class ListUsers:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self) -> list[UserView]:
        now = self._clock.now()
        async with self._uow() as uow:
            users = await uow.users.list_all()
        return [UserView.of(u, now) for u in sorted(users, key=lambda u: u.name.lower())]


class CreateUser:
    def __init__(self, uow: UowFactory, hasher: PasswordHasher, policy: PasswordPolicy, clock: Clock) -> None:
        self._uow, self._hasher, self._policy, self._clock = uow, hasher, policy, clock

    async def __call__(
        self, actor: Principal, *, email: str, name: str, roles: frozenset[Role], temporary_password: str
    ) -> UserView:
        email = normalise_email(email)
        self._policy.validate(temporary_password, email=email)
        now = self._clock.now()
        async with self._uow() as uow:
            if await uow.users.get_by_email(email):
                raise EmailAlreadyRegisteredError()
            user = User.register(
                email=email,
                name=name,
                password_hash=self._hasher.hash(temporary_password),
                roles=roles,
                now=now,
            )
            await uow.users.add(user)
            uow.events.record(
                UserActivity(
                    user_id=user.id,
                    action="user_created",
                    detail=_roles_text(roles),
                    actor_role=_actor_role(actor),
                ),
                actor=actor.user_id,
            )
            await uow.commit()
            return UserView.of(user, now)


class UpdateUser:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(
        self,
        actor: Principal,
        user_id: str,
        *,
        name: str | None = None,
        roles: frozenset[Role] | None = None,
        active: bool | None = None,
    ) -> UserView:
        now = self._clock.now()
        async with self._uow() as uow:
            user = await _require(uow, user_id)
            loses_admin = (
                user.is_admin
                and user.active
                and ((roles is not None and Role.ADMIN not in roles) or active is False)
            )
            if user.id == actor.user_id and loses_admin:
                raise SelfLockoutError()
            if loses_admin and await uow.users.count_active_admins() <= 1:
                raise LastAdminError()
            changed = user.update_profile(now, name=name, roles=roles, active=active)
            if changed:
                await uow.users.save(user)
                detail = ", ".join(
                    f"roles → {_roles_text(user.roles)}"
                    if f == "roles"
                    else f"active → {user.active}"
                    if f == "active"
                    else f
                    for f in changed
                )
                uow.events.record(
                    UserActivity(
                        user_id=user.id, action="user_updated", detail=detail, actor_role=_actor_role(actor)
                    ),
                    actor=actor.user_id,
                )
                await uow.commit()
            return UserView.of(user, now)


class ResetMfa:
    """Forces the person to enrol a new authenticator at next sign-in (lost phone)."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, actor: Principal, user_id: str) -> UserView:
        now = self._clock.now()
        async with self._uow() as uow:
            user = await _require(uow, user_id)
            user.reset_mfa(now)
            await uow.users.save(user)
            uow.events.record(
                UserActivity(user_id=user.id, action="mfa_reset", actor_role=_actor_role(actor)),
                actor=actor.user_id,
            )
            await uow.commit()
            return UserView.of(user, now)


class ResetPassword:
    """Sets a temporary password that must be changed at next sign-in; also lifts a lockout."""

    def __init__(self, uow: UowFactory, hasher: PasswordHasher, policy: PasswordPolicy, clock: Clock) -> None:
        self._uow, self._hasher, self._policy, self._clock = uow, hasher, policy, clock

    async def __call__(self, actor: Principal, user_id: str, temporary_password: str) -> UserView:
        now = self._clock.now()
        async with self._uow() as uow:
            user = await _require(uow, user_id)
            self._policy.validate(temporary_password, email=user.email)
            user.change_password(self._hasher.hash(temporary_password), now, temporary=True)
            user.clear_failed_attempts(now)
            await uow.users.save(user)
            uow.events.record(
                UserActivity(user_id=user.id, action="password_reset", actor_role=_actor_role(actor)),
                actor=actor.user_id,
            )
            await uow.commit()
            return UserView.of(user, now)


class EnsureBootstrapAdmin:
    """Creates the first admin from configuration when the user table is empty (first start only)."""

    def __init__(self, uow: UowFactory, hasher: PasswordHasher, policy: PasswordPolicy, clock: Clock) -> None:
        self._uow, self._hasher, self._policy, self._clock = uow, hasher, policy, clock

    async def __call__(self, email: str, password: str, name: str = "Administrator") -> bool:
        async with self._uow() as uow:
            if await uow.users.list_all():
                return False
            self._policy.validate(password, email=email)
            user = User.register(
                email=email,
                name=name,
                password_hash=self._hasher.hash(password),
                roles=frozenset({Role.ADMIN}),
                now=self._clock.now(),
                password_change_required=False,
            )
            await uow.users.add(user)
            uow.events.record(
                UserActivity(user_id=user.id, action="user_created", detail="bootstrap admin"), actor="system"
            )
            await uow.commit()
            return True


async def _require(uow: UnitOfWork, user_id: str) -> User:
    user = await uow.users.get(user_id)
    if user is None:
        raise UserNotFoundError()
    return user


def _roles_text(roles: frozenset[Role]) -> str:
    return ", ".join(sorted(r.value for r in roles)) or "none"


def _actor_role(actor: Principal) -> str | None:
    role = actor.primary_role
    return role.value if role else None
