"""Data subject requests (GDPR Art. 15 to 21): what kind, how long we have, and which steps are allowed.
Pure.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from opspilot_common.auth.roles import Role

from .errors import InvalidRequestError, NotAllowedError

# Only the compliance lead handles data requests: they can release or destroy a person's data.
DSR_ROLE = Role.COMPLIANCE_LEAD
# Services that acknowledge an erasure. The audit log is not among them: it keeps its entries, tokens only.
ERASING_STORES = ("mail", "orchestrator", "approval", "jira", "reporting")
EXPORT, CONFIRMATION = "export", "erasure_confirmation"
DUE_SOON = timedelta(days=7)


class DsrType(StrEnum):
    ACCESS = "access"
    RECTIFICATION = "rectification"
    ERASURE = "erasure"
    RESTRICTION = "restriction"
    OBJECTION = "objection"


LABELS = {
    DsrType.ACCESS: ("Access", "Art. 15 right of access and Art. 20 portability"),
    DsrType.RECTIFICATION: ("Rectification", "Art. 16 right to rectification"),
    DsrType.ERASURE: ("Erasure", "Art. 17 right to erasure"),
    DsrType.RESTRICTION: ("Restriction", "Art. 18 right to restriction"),
    DsrType.OBJECTION: ("Objection", "Art. 21 right to object"),
}


class DsrStatus(StrEnum):
    OPEN = "open"
    SENDING = "sending"  # export decided; mail-service is sending it
    ERASING = "erasing"  # erasure decided; waiting for the stores to acknowledge
    CONFIRMING = "confirming"  # all stores done; mail-service is sending the confirmation
    SEND_FAILED = "send_failed"  # the email did not go out: the compliance lead decides what next
    COMPLETED = "completed"


def add_month(day: date) -> date:
    """One calendar month (Art. 12(3)), the last day of a shorter month when there is no such date."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(slots=True)
class DataRequest:
    id: str  # DSR-0001
    type: DsrType
    status: DsrStatus
    received_on: date
    due_on: date
    created_at: datetime
    address: str | None = field(default=None, repr=False)  # encrypted at rest; gone when the request closes
    subject_token: str | None = None  # None: the address is not in the vault, we hold nothing
    verified_how: str | None = None
    verified_at: datetime | None = None
    pending_stores: list[str] = field(default_factory=list)
    # After erasure: tokens and ids only, so the screen can still show what the audit log keeps about them.
    erased: dict[str, list[str]] = field(default_factory=dict)
    attempt: int = 1
    outcome: str | None = None
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status is not DsrStatus.COMPLETED

    def verify(self, how: str, now: datetime) -> None:
        self._must_be(DsrStatus.OPEN)
        how = " ".join(how.split())
        if not 5 <= len(how) <= 300:
            raise InvalidRequestError("Say how the requester's identity was checked (5 to 300 characters).")
        self.verified_how, self.verified_at = how, now

    def start_export(self) -> None:
        self._ready(DsrType.ACCESS)
        self.status = DsrStatus.SENDING

    def start_erasure(self, tokens: list[str], email_ids: list[str], run_ids: list[str]) -> None:
        self._ready(DsrType.ERASURE)
        self.status, self.pending_stores = DsrStatus.ERASING, list(ERASING_STORES)
        self.erased = {"tokens": tokens, "own_email_ids": email_ids, "run_ids": run_ids}

    def store_erased(self, store: str) -> bool:
        """True when this was the last store: the confirmation can go out."""
        if self.status is not DsrStatus.ERASING or store not in self.pending_stores:
            return False
        self.pending_stores.remove(store)
        if self.pending_stores:
            return False
        self.status = DsrStatus.CONFIRMING
        return True

    def retry_send(self) -> str:
        self._must_be(DsrStatus.SEND_FAILED)
        self.attempt += 1
        if self.type is DsrType.ACCESS:
            self.status = DsrStatus.SENDING
            return EXPORT
        self.status = DsrStatus.CONFIRMING
        return CONFIRMATION

    def message_result(self, sent: bool, detail: str | None, now: datetime) -> bool:
        """True when this closed the request. A result for an earlier attempt changes nothing."""
        if self.status not in (DsrStatus.SENDING, DsrStatus.CONFIRMING):
            return False
        if not sent:
            self.status, self.outcome = DsrStatus.SEND_FAILED, detail or "The email did not go out."
            return False
        erased = self.status is DsrStatus.CONFIRMING
        self.close(
            "Erased; confirmation sent to the requester." if erased else "Data sent to the verified address.",
            now,
        )
        return True

    def close_by_hand(self, outcome: str, now: datetime) -> None:
        self._must_be(DsrStatus.OPEN)
        outcome = " ".join(outcome.split())
        if not 5 <= len(outcome) <= 500:
            raise InvalidRequestError("Record what was done or why it was refused (5 to 500 characters).")
        self.close(outcome, now)

    def close(self, outcome: str, now: datetime) -> None:
        self.status, self.outcome, self.closed_at, self.address = DsrStatus.COMPLETED, outcome, now, None

    def _ready(self, kind: DsrType) -> None:
        self._must_be(DsrStatus.OPEN)
        if self.type is not kind:
            raise InvalidRequestError(f"This is a {self.type.value} request, not a {kind.value} request.")
        if self.verified_at is None:
            raise NotAllowedError("Verify the requester's identity first.")

    def _must_be(self, status: DsrStatus) -> None:
        if self.status is not status:
            raise InvalidRequestError(
                f"Request {self.id} is {self.status.value}: this step is not available."
            )


def due_label(request: DataRequest, today: date) -> tuple[str, str]:
    """(text, level) for the list: level is ok, soon or late."""
    if not request.is_open:
        return (f"closed {request.closed_at:%-d %b}" if request.closed_at else "closed"), "closed"
    days = (request.due_on - today).days
    if days < 0:
        return f"{-days} day{'s' if days < -1 else ''} overdue", "late"
    level = "soon" if timedelta(days=days) <= DUE_SOON else "ok"
    return f"{days} day{'s' if days != 1 else ''} left", level


def package_notes() -> list[str]:
    return [
        "Items such as [PERSON_2] or [EMAIL_3] stand for other people. They are left out to protect their "
        "rights (Art. 15(4) GDPR).",
        "Emails you wrote appear in full. Emails that only mention you are counted, not copied.",
    ]
