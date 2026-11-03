"""Turning tokens back into people: a person's audited 5-minute reveal, and detokenising for sending."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from opspilot_common.auth.principal import Principal
from opspilot_common.events.catalogue import PrivacyRecorded

from ..domain.errors import InvalidRequestError, NotAllowedError, UnknownTokenError
from ..domain.records import DETOKENISE_SERVICES, REVEAL_DURATION, REVEAL_ROLES, Reveal
from ..domain.tokens import TOKEN_IN_TEXT, parse_token, tokens_in
from ..ports import Clock, UnitOfWork

UowFactory = Callable[[], UnitOfWork]


class RevealIdentity:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(
        self, principal: Principal, token_text: str, reason: str, thread_id: str | None = None
    ) -> Reveal:
        if not principal.has_any(*REVEAL_ROLES):
            raise NotAllowedError("Your role cannot see customer identities.")
        reason = " ".join(reason.split())
        if not 5 <= len(reason) <= 300:
            raise InvalidRequestError("Say why you need to see this identity (5 to 300 characters).")
        token = parse_token(token_text)
        if token is None:
            raise InvalidRequestError(f"'{token_text}' is not a token such as [PERSON_1].")
        minutes = int(REVEAL_DURATION.total_seconds() // 60)
        role = principal.primary_role.value if principal.primary_role else "unknown"
        now = self._clock.now()
        async with self._uow() as uow:
            entry = await uow.vault.get(token)
            if entry is None:
                raise UnknownTokenError(token)
            values: dict[str, str] = {}
            for item in await uow.vault.same_subject(entry):
                values.setdefault(item.kind.value, item.value)
            reveal = Reveal(
                str(uuid.uuid4()),
                token,
                principal.user_id,
                role,
                reason,
                now,
                now + REVEAL_DURATION,
                thread_id,
                values,
            )
            await uow.reveals.add(reveal)
            uow.events.record(
                PrivacyRecorded(
                    action="reveal_identity",
                    subject_token=token,
                    detail=f"Viewed identity of [{token}] for {minutes} min: {reason}",
                    legal_basis="Art. 5(2) GDPR accountability",
                    thread_id=thread_id,
                    actor_role=role,
                ),
                actor=principal.user_id,
            )
            await uow.commit()
        return reveal


class Detokenise:
    """For sending an approved message only. An unknown token fails the whole request, so a customer never
    receives a literal `[PERSON_9]`."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, caller: str, text: str, *, purpose: str, thread_id: str | None = None) -> str:
        if caller not in DETOKENISE_SERVICES:
            raise NotAllowedError(f"Service {caller} may not turn tokens back into personal data.")
        tokens = tokens_in(text)
        listed = " ".join(f"[{t}]" for t in tokens)
        async with self._uow() as uow:
            values: dict[str, str] = {}
            for token in tokens:
                entry = await uow.vault.get(token)
                if entry is None:
                    raise UnknownTokenError(token)
                values[token] = entry.value
            uow.events.record(
                PrivacyRecorded(
                    action="detokenise",
                    detail=f"{len(tokens)} tokens resolved for {purpose}: {listed}",
                    legal_basis="Art. 6(1)(b) GDPR reply to the customer",
                    thread_id=thread_id,
                ),
                actor=caller,
            )
            await uow.commit()
        return TOKEN_IN_TEXT.sub(lambda m: values[f"{m.group(1)}_{m.group(2)}"], text)
