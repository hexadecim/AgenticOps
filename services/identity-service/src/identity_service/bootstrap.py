"""Composition root: the only module that knows every concrete adapter (Dependency Inversion)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from opspilot_common.auth import Principal, Role, TokenVerifier
from opspilot_common.resilience import ResilienceRegistry, redis_registry
from opspilot_common.web import principal_dependency, require_roles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.security import (
    Argon2PasswordHasher,
    FernetSecretCipher,
    JwtChallengeTokens,
    JwtSessionIssuer,
    PyOtpTotpProvider,
    SystemClock,
)
from .adapters.outbound.sql import IdentityTables, SqlUnitOfWork
from .application.auth import CompleteSignIn, GetCurrentUser, RecordSignOut, RenewSession, StartSignIn
from .application.users import (
    CreateUser,
    EnsureBootstrapAdmin,
    ListUsers,
    ResetMfa,
    ResetPassword,
    UpdateUser,
)
from .config import Settings
from .domain.policies import LockoutPolicy, PasswordPolicy
from .ports.repositories import UnitOfWork
from .ports.security import Clock


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: IdentityTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    start_sign_in: StartSignIn
    complete_sign_in: CompleteSignIn
    renew_session: RenewSession
    get_current_user: GetCurrentUser
    record_sign_out: RecordSignOut
    list_users: ListUsers
    create_user: CreateUser
    update_user: UpdateUser
    reset_mfa: ResetMfa
    reset_password: ResetPassword
    ensure_bootstrap_admin: EnsureBootstrapAdmin
    current_principal: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_admin: Callable[..., Awaitable[Principal]] = field(repr=False)


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
    schema: str | None = None,
) -> Container:
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = IdentityTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables)

    clock = clock or SystemClock()
    hasher, totp = Argon2PasswordHasher(), PyOtpTotpProvider(settings.totp_issuer)
    cipher = FernetSecretCipher(settings.fernet_key)
    challenges = JwtChallengeTokens(settings.jwt_secret, timedelta(minutes=settings.challenge_minutes))
    session_tokens = JwtSessionIssuer(settings.jwt_secret, timedelta(minutes=settings.session_minutes))
    lockout, password_policy = LockoutPolicy(), PasswordPolicy()
    registry = redis_registry(Redis.from_url(settings.redis_url), settings.resilience_config)
    current = principal_dependency(TokenVerifier(settings.jwt_secret))

    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        start_sign_in=StartSignIn(uow, hasher, totp, cipher, challenges, clock, lockout),
        complete_sign_in=CompleteSignIn(
            uow, hasher, totp, cipher, challenges, session_tokens, clock, lockout, password_policy
        ),
        renew_session=RenewSession(uow, session_tokens, clock),
        get_current_user=GetCurrentUser(uow, clock),
        record_sign_out=RecordSignOut(uow),
        list_users=ListUsers(uow, clock),
        create_user=CreateUser(uow, hasher, password_policy, clock),
        update_user=UpdateUser(uow, clock),
        reset_mfa=ResetMfa(uow, clock),
        reset_password=ResetPassword(uow, hasher, password_policy, clock),
        ensure_bootstrap_admin=EnsureBootstrapAdmin(uow, hasher, password_policy, clock),
        current_principal=current,
        require_admin=require_roles(current, Role.ADMIN),
    )
