"""Sending the answer to a data request. Nothing goes without `dsr.send_requested` from the compliance lead;
the address and content come from privacy-service, which checks the request's state again.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from opspilot_common.auth.roles import Role
from opspilot_common.events.catalogue import DsrMessageResult, DsrSendRequested
from opspilot_common.telemetry import current_traceparent, span

from ..domain.dsr_message import DsrMessage
from ..domain.errors import (
    DetokeniseRefusedError,
    DetokeniseUnavailableError,
    MailboxRejectedError,
    MailboxUnavailableError,
)
from ..domain.outgoing import OutgoingStatus
from ..ports import Clock, DsrContentSource, UnitOfWork
from .mailbox import ActiveMailbox

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]
ACTOR = "mail-service"
_UNKNOWN_OUTCOME = "Interrupted while sending; it may have gone out. Not sent again: check the Sent folder."


class QueueDsrMessage:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: DsrSendRequested) -> bool:
        if p.decided_by_role != Role.COMPLIANCE_LEAD.value:
            log.warning("dsr_send_refused", dsr=p.dsr_id, role=p.decided_by_role)
            return False
        message_id = f"{p.dsr_id}:{p.kind}:{p.attempt}"
        async with self._uow() as uow:
            if await uow.dsr_messages.exists(message_id):
                return False
            now = self._clock.now()
            await uow.dsr_messages.add(
                DsrMessage(message_id, p.dsr_id, p.kind, p.attempt, now, now, trace=current_traceparent())
            )
            await uow.commit()
        return True


class SendDsrMessages:
    def __init__(
        self,
        uow: UowFactory,
        active: ActiveMailbox,
        source: DsrContentSource,
        clock: Clock,
        *,
        batch: int = 5,
    ) -> None:
        self._uow, self._active, self._source, self._clock, self._batch = uow, active, source, clock, batch

    async def recover(self) -> int:
        async with self._uow() as uow:
            rows = await uow.dsr_messages.interrupted()
        for row in rows:
            row.failed(_UNKNOWN_OUTCOME)
            await self._finish(row)
        return len(rows)

    async def __call__(self) -> int:
        if self._active.current() is None:
            return 0
        async with self._uow() as uow:
            due = await uow.dsr_messages.due(self._clock.now(), self._batch)
        for row in due:
            with span("mail.send_dsr", traceparent=row.trace, dsr_id=row.dsr_id):
                await self._send_one(row)
        return len(due)

    async def _send_one(self, row: DsrMessage) -> None:
        current = self._active.current()
        sender = current[1].sender if current else None
        if sender is None:
            row.failed("The configured mailbox cannot send email.")
            await self._finish(row)
            return
        async with self._uow() as uow:
            claimed = await uow.dsr_messages.claim(row)
            await uow.commit()
        if not claimed:
            return  # another sender has it
        try:
            await sender.send(await self._source.outbound(row.dsr_id, row.kind))
        except (DetokeniseUnavailableError, MailboxUnavailableError) as exc:
            row.try_later(self._clock.now(), exc.message)
        except (DetokeniseRefusedError, MailboxRejectedError) as exc:
            row.failed(exc.message)
        except Exception as exc:
            log.exception("dsr_send_failed", dsr=row.dsr_id)
            row.failed(f"{_UNKNOWN_OUTCOME} ({type(exc).__name__})")
        else:
            row.sent()
        await self._finish(row)

    async def _finish(self, row: DsrMessage) -> None:
        async with self._uow() as uow:
            await uow.dsr_messages.save(row)
            if row.status is not OutgoingStatus.PENDING:
                uow.events.record(
                    DsrMessageResult(
                        dsr_id=row.dsr_id,
                        kind=row.kind,
                        attempt=row.attempt,
                        sent=row.status is OutgoingStatus.SENT,
                        detail=row.detail,
                    ),
                    actor=ACTOR,
                )
            await uow.commit()
