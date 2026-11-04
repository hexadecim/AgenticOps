"""Asks the other services what they hold on a person, in order: mail-service knows every email they wrote
(also those not tokenised yet), the orchestrator knows the runs on those emails, and the rest are keyed
by run. A store that cannot answer is reported as unavailable, so nothing reads as "none". The address
goes to mail-service only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import ResilienceError, ResilientHttpClient
from opspilot_common.subjects import StoreReport, SubjectQuery
from pydantic import ValidationError


@dataclass(frozen=True, slots=True)
class HttpStore:
    store: str
    label: str
    http: ResilientHttpClient


class HttpSubjectStores:
    def __init__(
        self, mail: HttpStore, runs: HttpStore, others: list[HttpStore], tokens: ServiceTokenIssuer
    ) -> None:
        self._mail, self._runs, self._others, self._tokens = mail, runs, others, tokens

    async def gather(self, query: SubjectQuery) -> list[StoreReport]:
        mail = await self._ask(self._mail, query)
        own = sorted({*query.own_email_ids, *mail.email_ids})
        keyed = query.model_copy(update={"address": None, "own_email_ids": own})
        runs = await self._ask(self._runs, keyed)
        keyed = keyed.model_copy(update={"run_ids": sorted({*query.run_ids, *runs.run_ids})})
        return [mail, runs, *await asyncio.gather(*[self._ask(s, keyed) for s in self._others])]

    async def _ask(self, store: HttpStore, query: SubjectQuery) -> StoreReport:
        try:
            response = await store.http.post(
                "/internal/v1/subject-data",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
                json=query.model_dump(),
            )
            if response.status_code == 200:
                return StoreReport.model_validate(response.json())
        except (ResilienceError, ValidationError):
            pass
        return StoreReport(store=store.store, label=store.label, what="could not be asked", available=False)
