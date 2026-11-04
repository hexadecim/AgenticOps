"""Raw emails are kept for a limited time only (GDPR storage limitation). Downstream work uses tokens."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import structlog
from opspilot_common.events.catalogue import PrivacyRecorded

from ..ports import Clock, UnitOfWork

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]


class PurgeExpiredEmails:
    def __init__(self, uow: UowFactory, clock: Clock, *, days: int = 30) -> None:
        self._uow, self._clock, self._days = uow, clock, days

    async def __call__(self) -> int:
        cutoff = self._clock.now() - timedelta(days=self._days)
        async with self._uow() as uow:
            deleted = await uow.emails.delete_fetched_before(cutoff)
            await uow.outgoing.delete_created_before(cutoff)  # tokenised; finished rows only
            if deleted:
                uow.events.record(
                    PrivacyRecorded(
                        action="retention_purge",
                        detail=f"Deleted {deleted} raw emails older than {self._days} days",
                        legal_basis="Art. 5(1)(e) GDPR storage limitation",
                    ),
                    actor="mail-service",
                )
            await uow.commit()
        if deleted:
            log.info("raw_emails_purged", deleted=deleted, days=self._days)
        return deleted
