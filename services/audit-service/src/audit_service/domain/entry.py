"""An audit entry: one event, described for people. Entries are never changed or deleted."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """The wireframe's tabs: People / Agents and tools / Privacy; `system` shows under All only."""

    HUMAN = "human"
    TOOL = "tool"
    PRIVACY = "privacy"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: str  # the event id: an event is recorded at most once
    occurred_at: datetime
    kind: Kind
    actor: str  # a role ("L1 Analyst") or an agent/service name, never a person's name
    event: str  # e.g. approve_ticket, pseudonymise, signed_in
    detail: str
    privacy: str  # what personal data this entry touched, and on what basis
    event_type: str
    source: str
    actor_id: str | None = None  # internal user id or service name, for investigations
    ticket: str | None = None
    thread_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)  # catalogue payloads hold ids and tokens only


@dataclass(frozen=True, slots=True)
class AuditQuery:
    kind: Kind | None = None
    text: str | None = None  # matches actor, event, detail, ticket, thread id (e.g. "[PERSON_1]", "KAN-12")
    before: tuple[datetime, str] | None = None  # keyset cursor: entries strictly older than this
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AuditPage:
    entries: list[AuditEntry]
    next_before: tuple[datetime, str] | None
