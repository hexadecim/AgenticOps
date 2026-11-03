"""Read-side use cases: the mailbox status for the console, and one stored email for internal callers."""

from __future__ import annotations

from collections.abc import Callable

from ..domain.errors import EmailNotFoundError
from ..domain.mail import StoredEmail
from ..domain.status import MailboxStatus, evaluate
from ..ports import CircuitProbe, Clock, UnitOfWork
from .mailbox import ActiveMailbox

UowFactory = Callable[[], UnitOfWork]


class GetMailboxStatus:
    def __init__(self, uow: UowFactory, active: ActiveMailbox, circuit: CircuitProbe, clock: Clock) -> None:
        self._uow, self._active, self._circuit, self._clock = uow, active, circuit, clock

    async def __call__(self) -> MailboxStatus:
        async with self._uow() as uow:
            state = await uow.poll_state.get()
        circuit, retry_after = await self._circuit.state()
        config = self._active.config
        return evaluate(
            state,
            now=self._clock.now(),
            configured=config is not None,
            provider=config.provider if config else None,
            folder=config.folder if config else None,
            interval=config.poll_interval_seconds if config else None,
            circuit=circuit,
            circuit_retry_after=retry_after,
        )


class GetEmail:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, email_id: str) -> StoredEmail:
        async with self._uow() as uow:
            email = await uow.emails.get(email_id)
        if email is None:
            raise EmailNotFoundError(email_id)
        return email
