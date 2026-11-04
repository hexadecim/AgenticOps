"""What a data subject has in a service's store (GDPR Art. 15 and 17). Each service answers for its own
schema; only privacy-service asks. The contract is shared so a new store is one adapter and one registry
line.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from .events.catalogue import SubjectErasureRequested


class SubjectQuery(BaseModel):
    tokens: list[str] = Field(default_factory=list, max_length=500)
    address: str | None = Field(default=None, description="For mail-service only: emails not tokenised yet")
    own_email_ids: list[str] = Field(default_factory=list, description="Emails this person wrote")
    mentioned_email_ids: list[str] = Field(default_factory=list, description="Others' emails naming them")
    run_ids: list[str] = Field(default_factory=list, description="Agent runs on their own emails")
    include_items: bool = False  # the rows themselves, for an export; otherwise counts only


class StoreReport(BaseModel):
    store: str
    label: str
    what: str
    count: int = 0
    erase_action: str = "Delete"
    available: bool = True
    email_ids: list[str] = Field(default_factory=list)  # mail: every email this person wrote
    run_ids: list[str] = Field(default_factory=list)  # orchestrator: runs on the person's emails
    open_runs: int = 0  # orchestrator: runs not finished yet
    busy_runs: int = 0  # orchestrator: runs being worked on this moment
    items: list[dict[str, Any]] = Field(default_factory=list)


class SubjectReporter(Protocol):
    async def report(self, query: SubjectQuery) -> StoreReport: ...


class SubjectStore(SubjectReporter, Protocol):
    async def erase(self, request: SubjectErasureRequested) -> None:
        """Delete and acknowledge (`subject.store_erased`) in one transaction. Raising lets RabbitMQ retry."""
        ...
