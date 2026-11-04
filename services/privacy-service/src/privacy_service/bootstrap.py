"""Composition root for privacy-service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opspilot_common.auth import AuthKeys, Principal
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient, redis_registry
from opspilot_common.web import principal_dependency, service_dependency
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .adapters.outbound.crypto import VaultCrypto
from .adapters.outbound.detectors import CompositeDetector
from .adapters.outbound.detectors.regex import RegexDetector
from .adapters.outbound.mail_http import MailServiceSource
from .adapters.outbound.sql import PrivacyTables, SqlUnitOfWork
from .adapters.outbound.subject_stores import HttpStore, HttpSubjectStores
from .adapters.outbound.system import SystemClock
from .application import (
    BuildOutbound,
    CloseRequest,
    Detokenise,
    GetRequest,
    ListRequests,
    LogRequest,
    PseudonymiseEmail,
    PurgeExpiredIdentities,
    RecordDsrMessage,
    RecordStoreErased,
    RetrySend,
    RevealIdentity,
    StartErasure,
    StartExport,
    VerifyRequest,
)
from .config import Settings
from .domain.records import DETOKENISE_SERVICES, PSEUDONYMISE_SERVICES
from .ports import Clock, EntityDetector, RawEmailSource, SubjectStores, UnitOfWork

SERVICE = "privacy-service"


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    tables: PrivacyTables
    sessions: async_sessionmaker  # type: ignore[type-arg]
    registry: ResilienceRegistry
    pseudonymise: PseudonymiseEmail
    reveal: RevealIdentity
    detokenise: Detokenise
    clock: Clock
    log_request: LogRequest
    list_requests: ListRequests
    get_request: GetRequest
    verify_request: VerifyRequest
    start_export: StartExport
    start_erasure: StartErasure
    close_request: CloseRequest
    retry_send: RetrySend
    record_store_erased: RecordStoreErased
    record_dsr_message: RecordDsrMessage
    build_outbound: BuildOutbound
    purge_identities: PurgeExpiredIdentities
    current_user: Callable[..., Awaitable[Principal]] = field(repr=False)
    require_service: Callable[..., Awaitable[str]] = field(repr=False)
    closers: list = field(default_factory=list)  # type: ignore[type-arg]

    def retention_schedule(self) -> list[dict[str, str]]:
        """What the system does today. "Not agreed" means nothing deletes it yet."""
        s = self.settings
        vault = f"{s.vault_retention_days} days after last use, no open run"
        not_agreed = "Kept. [agree with client]"
        return [
            {
                "what": "Token vault (real names and addresses)",
                "keep": vault if s.vault_purge_enabled else f"{vault} · job off",
            },
            {"what": "Raw email bodies", "keep": f"{s.raw_email_days} days"},
            {"what": "Audit log", "keep": "Kept, append-only. [per client policy]"},
            {"what": "Tokenised emails", "keep": not_agreed},
            {"what": "Thread checkpoints", "keep": not_agreed},
            {"what": "Data request records", "keep": "Kept; the address goes when the request closes"},
        ]


def default_detector(settings: Settings) -> EntityDetector:
    detectors: list[EntityDetector] = [RegexDetector()]
    if settings.ner_enabled:
        from .adapters.outbound.detectors.presidio import PresidioDetector, build_engine

        detectors.append(PresidioDetector(build_engine()))
    return CompositeDetector(detectors)


def build_container(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    schema: str | None = None,
    redis: Redis | None = None,
    source: RawEmailSource | None = None,
    detector: EntityDetector | None = None,
    clock: Clock | None = None,
    stores: SubjectStores | None = None,
) -> Container:
    keys = AuthKeys.load(SERVICE, settings.jwt_secret, settings.auth_keys_dir)
    engine = engine or create_async_engine(settings.database_url, pool_pre_ping=True)
    tables = PrivacyTables(schema=settings.db_schema if schema is None else schema or None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    crypto = VaultCrypto(settings.fernet_key)

    def uow() -> UnitOfWork:
        return SqlUnitOfWork(sessions, tables, crypto)

    redis = redis or Redis.from_url(settings.redis_url)
    registry = redis_registry(redis, settings.resilience_config)
    closers: list = [redis.aclose]  # type: ignore[type-arg]
    tokens = keys.service_issuer()

    def client(name: str, url: str) -> ResilientHttpClient:
        http = ResilientHttpClient.from_registry(registry, name, base_url=url)
        closers.append(http.aclose)
        return http

    mail_http = client("mail-service", settings.mail_url)
    if source is None:
        source = MailServiceSource(mail_http, tokens)
    if stores is None:
        # A new store: one line here, plus the shared route and consumer in that service.
        stores = HttpSubjectStores(
            HttpStore("mail", "Mail store", mail_http),
            HttpStore(
                "orchestrator", "Thread checkpoints", client("agent-orchestrator", settings.orchestrator_url)
            ),
            [
                HttpStore("approval", "Approvals", client("approval-service", settings.approval_url)),
                HttpStore("jira", "Jira fields", client("jira-connector", settings.jira_url)),
                HttpStore("reporting", "Dashboard", client("reporting-service", settings.reporting_url)),
                HttpStore("audit", "Audit log", client("audit-service", settings.audit_url)),
            ],
            tokens,
        )
    clock = clock or SystemClock()
    return Container(
        settings=settings,
        engine=engine,
        tables=tables,
        sessions=sessions,
        registry=registry,
        pseudonymise=PseudonymiseEmail(
            uow, source, detector or default_detector(settings), RegexDetector(), clock
        ),
        reveal=RevealIdentity(uow, clock),
        detokenise=Detokenise(uow),
        clock=clock,
        log_request=LogRequest(uow, clock),
        list_requests=ListRequests(uow),
        get_request=GetRequest(uow, stores),
        verify_request=VerifyRequest(uow, clock),
        start_export=StartExport(uow, stores),
        start_erasure=StartErasure(uow, stores),
        close_request=CloseRequest(uow, clock),
        retry_send=RetrySend(uow),
        record_store_erased=RecordStoreErased(uow),
        record_dsr_message=RecordDsrMessage(uow, clock),
        build_outbound=BuildOutbound(uow, stores, clock),
        purge_identities=PurgeExpiredIdentities(uow, stores, clock, days=settings.vault_retention_days),
        current_user=principal_dependency(keys.session_verifier()),
        # Use cases decide which service may do what; the route only requires a valid service token.
        require_service=service_dependency(
            keys.service_verifier(), *sorted(PSEUDONYMISE_SERVICES | DETOKENISE_SERVICES)
        ),
        closers=closers,
    )
