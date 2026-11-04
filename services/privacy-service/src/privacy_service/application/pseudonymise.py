"""PseudonymiseEmail: the only way email content reaches the agents. Fails closed at every step."""

from __future__ import annotations

import secrets
from collections.abc import Callable

import structlog
from opspilot_common.events.catalogue import PrivacyRecorded

from ..domain.entities import EntityKind, Identity, Span, clean_people, identity_spans, merge, normalise
from ..domain.errors import EmailNotFoundError, LeakDetectedError, NotAllowedError
from ..domain.records import PSEUDONYMISE_SERVICES, PseudonymisationRecord, TokenisedAddress, TokenisedEmail
from ..domain.text import guess_language, strip_quoted, strip_signature
from ..domain.tokens import bracket
from ..ports import Clock, EntityDetector, RawEmailSource, TokenVault, UnitOfWork

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]
_LEAK_KINDS = frozenset({EntityKind.EMAIL, EntityKind.PHONE, EntityKind.IBAN})
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _record_id() -> str:
    return "vlt-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


class _Tokens:
    """Per-request cache in front of the vault; header addresses define which person a name belongs to."""

    def __init__(self, vault: TokenVault) -> None:
        self._vault = vault
        self._cache: dict[tuple[EntityKind, str], str] = {}
        self.subjects: dict[str, str] = {}  # normalised address → that person's EMAIL token
        self.used: dict[EntityKind, set[str]] = {k: set() for k in EntityKind}

    async def get(self, kind: EntityKind, value: str, subject_key: str | None = None) -> str:
        key = (kind, normalise(kind, value))
        if key not in self._cache:
            subject = self.subjects.get(subject_key) if subject_key else None
            self._cache[key] = await self._vault.token_for(kind, value, subject=subject)
        self.used[kind].add(self._cache[key])
        return self._cache[key]

    async def address(self, ident: Identity) -> TokenisedAddress:
        email_token = await self.get(EntityKind.EMAIL, ident.email)
        self.subjects[normalise(EntityKind.EMAIL, ident.email)] = email_token
        name = ident.person_name
        if name is None:
            return TokenisedAddress(email_token)
        return TokenisedAddress(
            email_token, await self.get(EntityKind.PERSON, name, normalise(EntityKind.EMAIL, ident.email))
        )

    async def replace(self, text: str, spans: list[Span]) -> str:
        out, cursor = [], 0
        for span in spans:
            token = await self.get(span.kind, span.value, span.subject)
            out += [text[cursor : span.start], bracket(token)]
            cursor = span.end
        out.append(text[cursor:])
        return "".join(out)


class PseudonymiseEmail:
    def __init__(
        self,
        uow: UowFactory,
        source: RawEmailSource,
        detector: EntityDetector,
        leak_check: EntityDetector,
        clock: Clock,
    ) -> None:
        self._uow, self._source, self._detector, self._leak_check = uow, source, detector, leak_check
        self._clock = clock

    async def __call__(self, email_id: str, caller: str) -> PseudonymisationRecord:
        if caller not in PSEUDONYMISE_SERVICES:
            raise NotAllowedError(f"Service {caller} may not request pseudonymisation.")
        async with self._uow() as uow:
            if existing := await uow.records.by_email(email_id):
                return existing  # idempotent: same email, same tokens, no second audit entry
            if await uow.erased.contains(email_id):
                raise EmailNotFoundError(
                    email_id
                )  # its writer was erased; a queued run must not bring it back

        raw = await self._source.get(email_id)
        body, quoted = strip_quoted(raw.body)
        body, signature = strip_signature(body)
        language = guess_language(f"{raw.subject}\n{body}")
        identities = [i for i in (raw.sender, *raw.to, *raw.cc) if i.email]
        fields = {"subject": raw.subject, "body": body}
        spans = {
            name: merge(
                identity_spans(text, identities)
                + clean_people(text, await self._detector.detect(text, language))
            )
            for name, text in fields.items()
        }

        async with self._uow() as uow:
            tokens = _Tokens(uow.vault)
            sender = await tokens.address(raw.sender)
            to = tuple([await tokens.address(i) for i in raw.to if i.email])
            cc = tuple([await tokens.address(i) for i in raw.cc if i.email])
            subject = await tokens.replace(raw.subject, spans["subject"])
            body = await tokens.replace(body, spans["body"])
            attachments = []
            for item in raw.attachments:  # file names can name people too ("Alex_Muster_CV.pdf")
                name = str(item.get("filename", ""))
                found = identity_spans(name.replace("_", " "), identities) + await self._leak_check.detect(
                    name, language
                )
                attachments.append({**item, "filename": await tokens.replace(name, merge(found))})
            await self._check_no_leaks(subject, body, language)

            email = TokenisedEmail(
                email_id,
                raw.thread_id,
                sender,
                to,
                cc,
                subject,
                body,
                raw.received_at,
                language,
                tuple(attachments),
            )
            record = PseudonymisationRecord(
                id=_record_id(),
                email_id=email_id,
                thread_id=raw.thread_id,
                counts={k.value: len(v) for k, v in tokens.used.items() if v},
                signature_removed=signature,
                quoted_removed=quoted,
                email=email,
                created_at=self._clock.now(),
            )
            await uow.records.add(record)
            uow.events.record(
                PrivacyRecorded(
                    action="pseudonymise",
                    subject_token=sender.email_token,
                    detail=f"Email in thread {raw.thread_id[:8]}: {record.summary()}",
                    legal_basis="Art. 32 GDPR pseudonymisation",
                    thread_id=raw.thread_id,
                    record_id=record.id,
                    email_id=email_id,
                ),
                actor="privacy-service",
            )
            await uow.commit()
        log.info("email_pseudonymised", email_id=email_id, record=record.id, counts=record.counts)
        return record

    async def _check_no_leaks(self, subject: str, body: str, language: str) -> None:
        """Last line of defence: what pattern detection can still find in the output must not leave."""
        leaks = sorted(
            {s.kind.value for text in (subject, body) for s in await self._leak_check.detect(text, language)}
            & {k.value for k in _LEAK_KINDS}
        )
        if leaks:
            raise LeakDetectedError(leaks)
