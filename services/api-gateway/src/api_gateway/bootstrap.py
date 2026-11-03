"""Composition root for the gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opspilot_common.auth import Principal, Role, TokenVerifier
from opspilot_common.resilience import (
    ResilienceRegistry,
    ResilienceSettings,
    ResilientHttpClient,
    default_is_failure,
    redis_registry,
)
from opspilot_common.web import principal_dependency, require_roles
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .adapters.inbound.live import Broadcaster
from .adapters.outbound.approvals_http import (
    ApprovalHttpClient,
    OpsHttpClient,
    ReportingHttpClient,
    RunsHttpClient,
)
from .adapters.outbound.audit_privacy_http import AuditHttpClient, PrivacyHttpClient
from .adapters.outbound.breakers import RedisBreakerStateReader
from .adapters.outbound.identity_http import IdentityHttpClient
from .adapters.outbound.mail_http import MailHttpClient
from .adapters.outbound.rate_limit import RedisRateLimiter
from .adapters.outbound.settings_http import SettingsHttpClient
from .application import SignIn, SystemHealth
from .config import Settings
from .ports import (
    ApprovalPort,
    AuditPort,
    IdentityPort,
    MailPort,
    OpsPort,
    PrivacyPort,
    RateLimiter,
    ReportingPort,
    RunsPort,
    SettingsPort,
)


@dataclass
class Container:
    settings: Settings
    registry: ResilienceRegistry
    identity: IdentityPort
    settings_api: SettingsPort
    mail: MailPort
    audit: AuditPort
    privacy: PrivacyPort
    approvals: ApprovalPort
    runs: RunsPort
    reporting: ReportingPort
    ops: OpsPort
    live: Broadcaster
    verifier: TokenVerifier
    sign_in: SignIn
    health: SystemHealth
    require_admin: Callable[..., Awaitable[Principal]]
    closers: list = field(default_factory=list)  # type: ignore[type-arg]


def _is_failure(exc: BaseException) -> bool:
    return default_is_failure(exc) or isinstance(exc, RedisError)


def build_container(
    settings: Settings,
    *,
    identity: IdentityPort | None = None,
    settings_api: SettingsPort | None = None,
    mail: MailPort | None = None,
    audit: AuditPort | None = None,
    privacy: PrivacyPort | None = None,
    approvals: ApprovalPort | None = None,
    runs: RunsPort | None = None,
    reporting: ReportingPort | None = None,
    ops: OpsPort | None = None,
    limiter: RateLimiter | None = None,
    redis: Redis | None = None,
) -> Container:
    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config, is_failure=_is_failure)
    closers: list = [redis.aclose]  # type: ignore[type-arg]

    def service(name: str, url: str) -> ResilientHttpClient:
        http = ResilientHttpClient.from_registry(registry, name, base_url=url)
        closers.append(http.aclose)
        return http

    identity = identity or IdentityHttpClient(service("identity-service", settings.identity_url))
    settings_api = settings_api or SettingsHttpClient(service("settings-service", settings.settings_url))
    mail = mail or MailHttpClient(service("mail-service", settings.mail_url))
    audit = audit or AuditHttpClient(service("audit-service", settings.audit_url))
    privacy = privacy or PrivacyHttpClient(service("privacy-service", settings.privacy_url))
    approvals = approvals or ApprovalHttpClient(service("approval-service", settings.approval_url))
    runs = runs or RunsHttpClient(service("agent-orchestrator", settings.orchestrator_url))
    reporting = reporting or ReportingHttpClient(service("reporting-service", settings.reporting_url))
    ops = ops or OpsHttpClient(service("ops-service", settings.ops_url))
    limiter = limiter or RedisRateLimiter(redis, registry.get("redis"))
    resilience = (
        ResilienceSettings.from_yaml(settings.resilience_config)
        if settings.resilience_config
        else ResilienceSettings()
    )
    current = principal_dependency(TokenVerifier(settings.jwt_secret))
    return Container(
        settings=settings,
        registry=registry,
        identity=identity,
        settings_api=settings_api,
        mail=mail,
        audit=audit,
        privacy=privacy,
        approvals=approvals,
        runs=runs,
        reporting=reporting,
        ops=ops,
        live=Broadcaster(),
        verifier=TokenVerifier(settings.jwt_secret),
        sign_in=SignIn(identity, limiter, settings.sign_in_limit_per_minute),
        health=SystemHealth(RedisBreakerStateReader(redis), resilience),
        require_admin=require_roles(current, Role.ADMIN),
        closers=closers,
    )
