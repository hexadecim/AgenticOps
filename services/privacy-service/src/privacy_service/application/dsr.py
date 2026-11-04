"""Data subject requests: log, verify, export, erase, close. Only the compliance lead acts; every step is
audited.

The person is found by the keyed hash of their address, and the export goes only to that stored address.
Erasure starts here (vault and tokenised emails in one transaction, so the tokens die at once) and
finishes when every store has acknowledged; only then does the confirmation go out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from opspilot_common.auth.principal import Principal
from opspilot_common.events.catalogue import (
    DsrMessageResult,
    DsrSendRequested,
    PrivacyRecorded,
    SubjectErasureRequested,
    SubjectStoreErased,
)
from opspilot_common.subjects import StoreReport, SubjectQuery

from ..domain.dsr import (
    CONFIRMATION,
    DSR_ROLE,
    EXPORT,
    LABELS,
    DataRequest,
    DsrStatus,
    DsrType,
    add_month,
    package_notes,
)
from ..domain.entities import EntityKind
from ..domain.errors import (
    InvalidRequestError,
    NotAllowedError,
    RequestNotFoundError,
    StoresUnavailableError,
    SubjectBusyError,
)
from ..domain.records import PseudonymisationRecord
from ..ports import Clock, SubjectStores, UnitOfWork, VaultEntry

UowFactory = Callable[[], UnitOfWork]
ACTOR = "privacy-service"
BASIS = {
    DsrType.ACCESS: "Art. 15 GDPR",
    DsrType.RECTIFICATION: "Art. 16 GDPR",
    DsrType.ERASURE: "Art. 17 GDPR",
    DsrType.RESTRICTION: "Art. 18 GDPR",
    DsrType.OBJECTION: "Art. 21 GDPR",
}


def _role(principal: Principal) -> str:
    if not principal.has_any(DSR_ROLE):
        raise NotAllowedError("Only the compliance lead can handle data requests.")
    return DSR_ROLE.value


async def _get(uow: UnitOfWork, dsr_id: str) -> DataRequest:
    request = await uow.requests.get(dsr_id)
    if request is None:
        raise RequestNotFoundError(dsr_id)
    return request


async def _for_change(uow: UnitOfWork, dsr_id: str) -> DataRequest:
    """Held until the transaction ends: a double click or five stores answering at once are served in turn."""
    request = await uow.requests.get_for_update(dsr_id)
    if request is None:
        raise RequestNotFoundError(dsr_id)
    return request


@dataclass(slots=True)
class Subject:
    tokens: list[str] = field(default_factory=list)
    entries: list[VaultEntry] = field(default_factory=list)
    own: list[PseudonymisationRecord] = field(default_factory=list)
    mentioned: list[PseudonymisationRecord] = field(default_factory=list)
    address: str | None = field(default=None, repr=False)

    def query(self, *, items: bool = False) -> SubjectQuery:
        return SubjectQuery(
            tokens=self.tokens,
            address=self.address,
            own_email_ids=[r.email_id for r in self.own],
            mentioned_email_ids=[r.email_id for r in self.mentioned],
            include_items=items,
        )

    def local_reports(self) -> list[StoreReport]:
        return [
            StoreReport(
                store="vault", label="Token vault", what="real name and address", count=len(self.entries)
            ),
            StoreReport(
                store="records",
                label="Tokenised emails",
                what="emails they wrote, names replaced",
                count=len(self.own),
            ),
        ]


async def load_subject(uow: UnitOfWork, token: str | None, address: str | None = None) -> Subject:
    """`address`: what the requester gave. Emails not tokenised yet are only findable by it."""
    if token is None or (entry := await uow.vault.get(token)) is None:
        return Subject(address=address)
    entries = await uow.vault.same_subject(entry)
    tokens = [e.token for e in entries]
    own: list[PseudonymisationRecord] = []
    mentioned: list[PseudonymisationRecord] = []
    for record in await uow.records.mentioning(tokens):
        (own if record.email.sender.email_token in tokens else mentioned).append(record)
    known = next((e.value for e in entries if e.kind is EntityKind.EMAIL), address)
    return Subject(tokens, entries, own, mentioned, known)


class LogRequest:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(
        self, principal: Principal, kind: str, address: str, received_on: date | None
    ) -> DataRequest:
        role = _role(principal)
        address = address.strip()
        if "@" not in address or len(address) > 320:
            raise InvalidRequestError("Enter the email address the requester is registered with.")
        try:
            dsr_type = DsrType(kind)
        except ValueError as exc:
            raise InvalidRequestError(f"Unknown request type {kind}.") from exc
        now = self._clock.now()
        received = received_on or now.date()
        if received > now.date():
            raise InvalidRequestError("The date received cannot be in the future.")
        async with self._uow() as uow:
            entry = await uow.vault.find(EntityKind.EMAIL, address)
            number = await uow.requests.next_number()
            request = DataRequest(
                f"DSR-{number:04d}",
                dsr_type,
                DsrStatus.OPEN,
                received,
                add_month(received),
                now,
                address=entry.value if entry else address,  # as on file, not as typed
                subject_token=entry.token if entry else None,
            )
            await uow.requests.add(request)
            uow.events.record(
                PrivacyRecorded(
                    action="dsr_logged",
                    subject_token=request.subject_token,
                    detail=f"{request.id}: {LABELS[dsr_type][0]} request logged, due {request.due_on}",
                    legal_basis=BASIS[dsr_type],
                    actor_role=role,
                ),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


class ListRequests:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, principal: Principal) -> list[DataRequest]:
        _role(principal)
        async with self._uow() as uow:
            return await uow.requests.all()


class GetRequest:
    """The request and where this person's data is held. A store that cannot answer is shown as such."""

    def __init__(self, uow: UowFactory, stores: SubjectStores) -> None:
        self._uow, self._stores = uow, stores

    async def __call__(self, principal: Principal, dsr_id: str) -> tuple[DataRequest, list[StoreReport]]:
        _role(principal)
        async with self._uow() as uow:
            request = await _get(uow, dsr_id)
            subject = await load_subject(uow, request.subject_token, request.address)
        query = SubjectQuery(**request.erased) if request.erased else subject.query()
        return request, [*subject.local_reports(), *await self._stores.gather(query)]


class VerifyRequest:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, principal: Principal, dsr_id: str, how: str) -> DataRequest:
        role = _role(principal)
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            request.verify(how, self._clock.now())
            await uow.requests.save(request)
            uow.events.record(
                PrivacyRecorded(
                    action="dsr_verified",
                    subject_token=request.subject_token,
                    detail=f"{request.id}: identity verified. {request.verified_how}",
                    legal_basis="Art. 12(6) GDPR",
                    actor_role=role,
                ),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


def _refuse_unavailable(reports: list[StoreReport]) -> None:
    down = [r.label for r in reports if not r.available]
    if down:
        raise StoresUnavailableError(down)


class StartExport:
    """The decision to send. The package is built when mail-service asks for it, and is never stored."""

    def __init__(self, uow: UowFactory, stores: SubjectStores) -> None:
        self._uow, self._stores = uow, stores

    async def __call__(self, principal: Principal, dsr_id: str) -> DataRequest:
        role = _role(principal)
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            subject = await load_subject(uow, request.subject_token, request.address)
        _refuse_unavailable(await self._stores.gather(subject.query()))
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            request.start_export()
            await uow.requests.save(request)
            uow.events.record(
                DsrSendRequested(dsr_id=dsr_id, kind=EXPORT, attempt=request.attempt, decided_by_role=role),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


class StartErasure:
    def __init__(self, uow: UowFactory, stores: SubjectStores) -> None:
        self._uow, self._stores = uow, stores

    async def __call__(self, principal: Principal, dsr_id: str) -> DataRequest:
        role = _role(principal)
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            subject = await load_subject(uow, request.subject_token, request.address)
        reports = await self._stores.gather(subject.query())
        _refuse_unavailable(reports)
        busy = sum(r.busy_runs for r in reports)
        if busy:
            raise SubjectBusyError(busy)
        run_ids = [run for r in reports for run in r.run_ids]
        own_ids = sorted({*(r.email_id for r in subject.own), *(e for r in reports for e in r.email_ids)})
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            request.start_erasure(subject.tokens, own_ids, run_ids)
            await uow.vault.erase(subject.tokens)
            await uow.records.delete_emails(own_ids)
            await uow.erased.add(own_ids)  # a run still queued must not pseudonymise them again
            await uow.requests.save(request)
            uow.events.record(
                SubjectErasureRequested(
                    dsr_id=dsr_id,
                    tokens=subject.tokens,
                    own_email_ids=own_ids,
                    run_ids=run_ids,
                    decided_by_role=role,
                ),
                actor=principal.user_id,
            )
            uow.events.record(
                PrivacyRecorded(
                    action="dsr_erasure",
                    subject_token=request.subject_token,
                    detail=f"{dsr_id}: {len(subject.tokens)} vault entries, {len(own_ids)} emails erased",
                    legal_basis=BASIS[DsrType.ERASURE],
                    actor_role=role,
                ),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


class CloseRequest:
    """Rectification, restriction and objection are carried out by a person and recorded here; any request
    may be refused with a reason (Art. 12(5))."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, principal: Principal, dsr_id: str, outcome: str) -> DataRequest:
        role = _role(principal)
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            request.close_by_hand(outcome, self._clock.now())
            await uow.requests.save(request)
            uow.events.record(
                PrivacyRecorded(
                    action="dsr_closed",
                    subject_token=request.subject_token,
                    detail=f"{dsr_id}: closed. {request.outcome}",
                    legal_basis=BASIS[request.type],
                    actor_role=role,
                ),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


class RetrySend:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, principal: Principal, dsr_id: str) -> DataRequest:
        role = _role(principal)
        async with self._uow() as uow:
            request = await _for_change(uow, dsr_id)
            kind = request.retry_send()
            await uow.requests.save(request)
            uow.events.record(
                DsrSendRequested(dsr_id=dsr_id, kind=kind, attempt=request.attempt, decided_by_role=role),
                actor=principal.user_id,
            )
            await uow.commit()
        return request


class RecordStoreErased:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: SubjectStoreErased) -> None:
        async with self._uow() as uow:
            request = await uow.requests.get_for_update(p.dsr_id)
            if request is None:
                return
            finished = request.store_erased(p.store)
            await uow.requests.save(request)
            if finished:
                uow.events.record(
                    DsrSendRequested(
                        dsr_id=p.dsr_id,
                        kind=CONFIRMATION,
                        attempt=request.attempt,
                        decided_by_role=DSR_ROLE.value,
                    ),
                    actor=ACTOR,
                )
            await uow.commit()


class RecordDsrMessage:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: DsrMessageResult) -> None:
        async with self._uow() as uow:
            request = await uow.requests.get_for_update(p.dsr_id)
            if request is None or p.attempt != request.attempt:
                return
            closed = request.message_result(p.sent, p.detail, self._clock.now())
            await uow.requests.save(request)
            if closed:
                action = "dsr_export" if p.kind == EXPORT else "dsr_erasure"
                uow.events.record(
                    PrivacyRecorded(
                        action=action,
                        subject_token=request.subject_token,
                        detail=f"{p.dsr_id}: completed. {request.outcome}",
                        legal_basis=BASIS[request.type],
                    ),
                    actor=ACTOR,
                )
            await uow.commit()


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    to: str
    subject: str
    body: str
    attachment_name: str | None = None
    attachment: str | None = field(default=None, repr=False)


_SIGNATURE = "OpsPilot Support"


class BuildOutbound:
    """For mail-service at the moment of sending. The state must match, so a decision cannot be replayed,
    and the address is the stored one: no caller can choose it."""

    def __init__(self, uow: UowFactory, stores: SubjectStores, clock: Clock) -> None:
        self._uow, self._stores, self._clock = uow, stores, clock

    async def __call__(self, dsr_id: str, kind: str) -> OutboundMessage:
        expected = {EXPORT: DsrStatus.SENDING, CONFIRMATION: DsrStatus.CONFIRMING}.get(kind)
        async with self._uow() as uow:
            request = await _get(uow, dsr_id)
            if expected is None or request.status is not expected or not request.address:
                raise NotAllowedError(f"{dsr_id} is not waiting for this message.")
            subject = await load_subject(uow, request.subject_token, request.address)
        if kind == CONFIRMATION:
            return OutboundMessage(
                request.address,
                f"Your data request {dsr_id}: erasure completed",
                f"Hello,\n\nWe have erased the personal data we held about you, as you asked ({dsr_id}).\n\n"
                "We keep the record of what was done, with your name and address replaced by a token that no "
                "longer leads to you. We must keep it to show that we handled your data lawfully.\n\n"
                f"{_SIGNATURE}",
            )
        return await self._export(request, request.address, subject)

    async def _export(self, request: DataRequest, address: str, subject: Subject) -> OutboundMessage:
        query = subject.query(items=True)
        reports = await self._stores.gather(query)
        _refuse_unavailable(reports)
        package: dict[str, Any] = {
            "request": {
                "id": request.id,
                "received_on": request.received_on.isoformat(),
                "generated_at": self._clock.now().isoformat(),
            },
            "notes": package_notes(),
            "about_you": [{"kind": e.kind.value.lower(), "value": e.value} for e in subject.entries],
            "stores": {
                r.store: {"what": r.what, "count": r.count, "items": r.items}
                for r in [*subject.local_reports(), *reports]
            },
        }
        package["stores"]["records"]["items"] = [
            {
                "email_id": r.email_id,
                "subject": r.email.subject,
                "body": r.email.body,
                "received_at": r.email.received_at.isoformat(),
            }
            for r in subject.own
        ]
        held = "the data we hold about you"
        return OutboundMessage(
            address,
            f"Your data request {request.id}: your data",
            f"Hello,\n\nAttached is {held}, in a machine-readable JSON file, as you asked ({request.id}).\n\n"
            f"{_SIGNATURE}",
            f"{request.id.lower()}.json",
            json.dumps(package, ensure_ascii=False, indent=2),
        )


class PurgeExpiredIdentities:
    """Storage limitation for the vault: a person's real values go once none of their tokens has been used
    for the retention period and no run of theirs is open. If the orchestrator cannot say, nothing goes."""

    def __init__(
        self, uow: UowFactory, stores: SubjectStores, clock: Clock, *, days: int, batch: int = 50
    ) -> None:
        self._uow, self._stores, self._clock, self._days, self._batch = uow, stores, clock, days, batch

    async def __call__(self) -> int:
        cutoff = self._clock.now() - timedelta(days=self._days)
        async with self._uow() as uow:
            tokens = await uow.vault.unused_since(cutoff, self._batch)
        purged = 0
        for token in tokens:
            async with self._uow() as uow:
                subject = await load_subject(uow, token)
            if not subject.tokens:
                continue
            reports = await self._stores.gather(subject.query())
            if any(not r.available or r.open_runs for r in reports):
                continue
            async with self._uow() as uow:
                await uow.vault.erase(subject.tokens)
                purged += 1
                uow.events.record(
                    PrivacyRecorded(
                        action="retention_purge",
                        subject_token=token,
                        detail=f"Deleted {len(subject.tokens)} vault entries unused for {self._days} days",
                        legal_basis="Art. 5(1)(e) GDPR storage limitation",
                    ),
                    actor=ACTOR,
                )
                await uow.commit()
        return purged
