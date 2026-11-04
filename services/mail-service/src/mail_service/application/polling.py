"""PollMailbox: read new mail, store it once, group it into threads, announce it with `email.received`."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import structlog
from opspilot_common.events.catalogue import EmailReceived
from opspilot_common.telemetry import span

from ..domain.errors import MailboxRejectedError, MailboxUnavailableError, SettingsUnavailableError
from ..domain.mail import IncomingMail, StoredEmail
from ..ports import Clock, EmailRepository, UnitOfWork
from .mailbox import ActiveMailbox, ApplyMailboxSettings

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]


class Outcome(StrEnum):
    POLLED = "polled"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PollResult:
    outcome: Outcome
    new: int = 0
    detail: str = ""


class PollMailbox:
    """One poll cycle. A provider failure skips the cycle (recorded for the status), never raises.

    Each batch is stored in one transaction together with its `email.received` outbox rows and the new read
    position, so a crash re-reads the batch and the dedupe key prevents a second copy.
    """

    def __init__(
        self,
        uow: UowFactory,
        active: ActiveMailbox,
        apply_settings: ApplyMailboxSettings,
        clock: Clock,
        *,
        batch_size: int = 25,
        max_batches: int = 4,
        initial_lookback: timedelta = timedelta(hours=24),
    ) -> None:
        self._uow, self._active, self._apply, self._clock = uow, active, apply_settings, clock
        self._batch_size, self._max_batches, self._lookback = batch_size, max_batches, initial_lookback
        self._lock = asyncio.Lock()

    async def __call__(self) -> PollResult:
        async with self._lock:  # the scheduler and "Check now" never poll concurrently
            if not self._active.loaded:
                try:
                    await self._apply()
                except SettingsUnavailableError as exc:
                    return PollResult(Outcome.FAILED, detail=exc.message)
            current = self._active.current()
            if current is None:
                return PollResult(Outcome.NOT_CONFIGURED)
            config, connection = current
            total = 0
            for _ in range(self._max_batches):
                now = self._clock.now()
                async with self._uow() as uow:
                    state = await uow.poll_state.get()
                try:
                    batch = await connection.reader.fetch_new(
                        state.cursor_for(config.fingerprint),
                        limit=self._batch_size,
                        since=now - self._lookback,
                    )
                except (MailboxUnavailableError, MailboxRejectedError) as exc:
                    async with self._uow() as uow:
                        state = await uow.poll_state.get()
                        state.failed(now, exc.message)
                        await uow.poll_state.save(state)
                        await uow.commit()
                    log.warning("mailbox_poll_failed", provider=config.provider, error=exc.message)
                    return PollResult(Outcome.FAILED, total, exc.message)
                try:
                    async with self._uow() as uow:
                        new = 0
                        for mail in batch.mails:
                            new += await self._store(uow, connection.provider, mail, now)
                        state = await uow.poll_state.get()
                        state.succeeded(now, config.fingerprint, batch.cursor, new)
                        await uow.poll_state.save(state)
                        await uow.commit()
                except Exception as exc:  # e.g. a message the database rejects: make it visible, not silent
                    log.exception("mailbox_store_failed", provider=config.provider)
                    reason = f"A message could not be stored ({type(exc).__name__}); reading is paused at it."
                    async with self._uow() as uow:
                        state = await uow.poll_state.get()
                        state.failed(now, reason)
                        await uow.poll_state.save(state)
                        await uow.commit()
                    return PollResult(Outcome.FAILED, total, reason)
                total += new
                if not batch.more:
                    break
            if total:
                log.info("mailbox_polled", provider=config.provider, new=total)
            return PollResult(Outcome.POLLED, total)

    async def _store(self, uow: UnitOfWork, provider: str, mail: IncomingMail, now: datetime) -> int:
        key = mail.dedupe_key(provider)
        if await uow.emails.exists(key):
            return 0
        thread_id = await _thread_for(uow.emails, provider, mail) or str(uuid.uuid4())
        email = StoredEmail(str(uuid.uuid4()), thread_id, provider, now, mail)
        await uow.emails.add(email)
        # Ids only: the content stays here until privacy-service has pseudonymised it.
        with span("email.received", email_id=email.id):  # a new mail starts the case's trace
            uow.events.record(
                EmailReceived(
                    email_id=email.id, thread_id=thread_id, provider=provider, received_at=mail.received_at
                ),
                actor="mail-service",
            )
        return 1


async def _thread_for(emails: EmailRepository, provider: str, mail: IncomingMail) -> str | None:
    """Replies join their parent's thread (In-Reply-To / References), else the provider's conversation."""
    parents = mail.parent_ids()
    if parents and (thread := await emails.thread_of_message(parents)):
        return thread
    if mail.provider_thread_id:
        return await emails.thread_of_provider_thread(provider, mail.provider_thread_id)
    return None
