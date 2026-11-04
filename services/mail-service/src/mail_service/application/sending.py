"""Sending approved customer messages. Nothing leaves without `approval.decided` from an allowed role,
whatever `message.send.requested` says: mail-service sees the decision itself, as jira-connector and
ops-service do."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog
from opspilot_common.auth.roles import GATE_APPROVERS, Role
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    Decision,
    Gate,
    MessageFailed,
    MessageSendRequested,
    MessageSent,
)
from opspilot_common.telemetry import current_traceparent, span

from ..domain.errors import (
    DetokeniseRefusedError,
    DetokeniseUnavailableError,
    EmailNotFoundError,
    MailboxRejectedError,
    MailboxUnavailableError,
)
from ..domain.mail import Address, OutgoingMail
from ..domain.outgoing import OutgoingMessage, OutgoingStatus
from ..ports import Clock, Detokeniser, UnitOfWork
from .mailbox import ActiveMailbox

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]
ACTOR = "mail-service"
_UNKNOWN_OUTCOME = "Interrupted while sending; it may have gone out. Not sent again: check the Sent folder."


class QueueMessage:
    """`message.send.requested` → one row per recipient. A repeat of the event adds nothing."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: MessageSendRequested) -> bool:
        async with self._uow() as uow:
            if await uow.outgoing.exists(p.message_id):
                return False
            now = self._clock.now()
            for recipient in p.recipients:
                await uow.outgoing.add(
                    OutgoingMessage(
                        id=str(uuid.uuid4()), message_id=p.message_id, approval_id=p.approval_id,
                        run_id=p.run_id, thread_id=p.thread_id, email_id=recipient.email_id,
                        email_token=recipient.email_token, name_token=recipient.name_token,
                        subject=p.subject, body=p.body, created_at=now, next_try_at=now,
                        jira_key=p.jira_key, kind=p.kind, trace=current_traceparent(),
                    )
                )  # fmt: skip
            await uow.commit()
        return True


class RecordMessageApproval:
    """`approval.decided`: remember an approve at gate T3 by a role allowed to give it."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: ApprovalDecided) -> bool:
        if p.gate is not Gate.MESSAGE or p.decision is not Decision.APPROVE:
            return False
        try:
            allowed = Role(p.decided_by_role) in GATE_APPROVERS[Gate.MESSAGE]
        except ValueError:
            allowed = False
        if not allowed:
            log.warning("message_approval_refused", approval=p.approval_id, role=p.decided_by_role)
            return False
        async with self._uow() as uow:
            await uow.outgoing.approve(p.approval_id, self._clock.now())
            await uow.commit()
        return True


class SendDueMessages:
    """One step of the sender loop."""

    def __init__(
        self,
        uow: UowFactory,
        active: ActiveMailbox,
        privacy: Detokeniser,
        clock: Clock,
        *,
        batch: int = 20,
    ) -> None:
        self._uow, self._active, self._privacy, self._clock = uow, active, privacy, clock
        self._batch = batch

    async def recover(self) -> int:
        """At start-up: a row left `sending` may have gone out, so it is never sent again."""
        async with self._uow() as uow:
            rows = await uow.outgoing.interrupted()
        for row in rows:
            row.failed(_UNKNOWN_OUTCOME)
            await self._finish(row)
        return len(rows)

    async def __call__(self) -> int:
        if self._active.current() is None:
            return 0  # no mailbox configured yet: approved messages wait
        async with self._uow() as uow:
            due = await uow.outgoing.due(self._clock.now(), self._batch)
        for row in due:
            with span("mail.send", traceparent=row.trace, message_id=row.message_id):
                await self._send_one(row)
        return len(due)

    async def _send_one(self, row: OutgoingMessage) -> None:
        current = self._active.current()
        sender = current[1].sender if current else None
        if sender is None:
            row.failed("The configured mailbox cannot send email.")
            await self._finish(row)
            return
        async with self._uow() as uow:
            claimed = await uow.outgoing.claim(row)
            await uow.commit()
        if not claimed:
            return  # another sender has it: at most once, whatever the number of replicas
        try:
            await sender.send(await self._prepare(row))
        except (DetokeniseUnavailableError, MailboxUnavailableError) as exc:
            row.try_later(self._clock.now(), exc.message, retry_after=getattr(exc, "retry_after", None))
        except (DetokeniseRefusedError, MailboxRejectedError, EmailNotFoundError) as exc:
            row.failed(exc.message)
        except Exception as exc:
            log.exception("message_send_failed", message=row.message_id)
            row.failed(f"{_UNKNOWN_OUTCOME} ({type(exc).__name__})")
        else:
            row.sent()
        await self._finish(row)

    async def _finish(self, row: OutgoingMessage) -> None:
        """Save the row; when it was the message's last one, say how it went in the same transaction."""
        async with self._uow() as uow:
            await uow.outgoing.save(row)
            if row.status is not OutgoingStatus.PENDING:
                self._announce(uow, await uow.outgoing.of_message(row.message_id))
            await uow.commit()

    async def _prepare(self, row: OutgoingMessage) -> OutgoingMail:
        async with self._uow() as uow:
            original = await uow.emails.get(row.email_id)
        if original is None:
            raise EmailNotFoundError(row.email_id)
        lines = [f"[{row.email_token}]", f"[{row.name_token}]" if row.name_token else ""]
        address, name, body = (
            await self._privacy.detokenise("\n".join([*lines, row.personal_text()]), thread_id=row.thread_id)
        ).split("\n", 2)
        # The last guard on who receives it: only the person who wrote the email this message answers.
        if address.strip().lower() != original.mail.sender.email.strip().lower():
            raise MailboxRejectedError("The recipient is not the sender of the email this answers.")
        parent = original.mail.message_id
        return OutgoingMail(
            to=(Address(address.strip(), name.strip()),),
            subject=row.subject,
            body_text=body,
            in_reply_to=parent,
            references=original.mail.references,
        )

    def _announce(self, uow: UnitOfWork, rows: list[OutgoingMessage]) -> None:
        if not rows or any(r.status in (OutgoingStatus.PENDING, OutgoingStatus.SENDING) for r in rows):
            return
        first = rows[0]
        sent = [r.display_token for r in rows if r.status is OutgoingStatus.SENT]
        failed = [r.display_token for r in rows if r.status is OutgoingStatus.FAILED]
        reason = next((r.detail for r in rows if r.status is OutgoingStatus.FAILED and r.detail), None)
        if sent:
            uow.events.record(
                MessageSent(
                    message_id=first.message_id, thread_id=first.thread_id, recipient_tokens=sent,
                    jira_key=first.jira_key, run_id=first.run_id, approval_id=first.approval_id,
                    kind=first.kind, failed_tokens=failed, detail=reason,
                ),
                actor=ACTOR,
            )  # fmt: skip
        else:
            uow.events.record(
                MessageFailed(
                    message_id=first.message_id, run_id=first.run_id, thread_id=first.thread_id,
                    approval_id=first.approval_id, failed_tokens=failed,
                    reason=reason or "Not sent", jira_key=first.jira_key, kind=first.kind,
                ),
                actor=ACTOR,
            )  # fmt: skip
