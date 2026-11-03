"""Poll bookkeeping and the "Mailbox polled N min ago" status shown in the console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# A poll that has not succeeded for this many intervals is considered stale (amber).
STALE_AFTER_INTERVALS = 3


@dataclass(slots=True)
class PollState:
    """Where reading continues, and how the last polls went. One row per service (single mailbox)."""

    fingerprint: str | None = None
    cursor: dict[str, Any] | None = None
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_new_count: int = 0
    total_received: int = 0

    def cursor_for(self, fingerprint: str) -> dict[str, Any] | None:
        """The saved read position, unless it belongs to a different mailbox."""
        return self.cursor if self.fingerprint == fingerprint else None

    def succeeded(self, now: datetime, fingerprint: str, cursor: dict[str, Any], new: int) -> None:
        self.fingerprint, self.cursor = fingerprint, cursor
        self.last_poll_at = self.last_success_at = now
        self.last_error, self.consecutive_failures = None, 0
        self.last_new_count = new
        self.total_received += new

    def failed(self, now: datetime, reason: str) -> None:
        self.last_poll_at, self.last_error = now, reason
        self.consecutive_failures += 1


class Level(StrEnum):
    OK = "ok"
    WARNING = "warning"  # amber: provider paused, failing or stale
    OFF = "off"  # no mailbox configured


@dataclass(frozen=True, slots=True)
class MailboxStatus:
    level: Level
    message: str
    configured: bool
    provider: str | None
    folder: str | None
    poll_interval_seconds: int | None
    last_poll_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    circuit: str
    total_received: int


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def evaluate(
    state: PollState,
    *,
    now: datetime,
    configured: bool,
    provider: str | None,
    folder: str | None,
    interval: int | None,
    circuit: str,
    circuit_retry_after: float | None = None,
) -> MailboxStatus:
    def status(level: Level, message: str) -> MailboxStatus:
        return MailboxStatus(
            level,
            message,
            configured,
            provider,
            folder,
            interval,
            state.last_poll_at,
            state.last_success_at,
            state.last_error,
            circuit,
            state.total_received,
        )

    if not configured:
        return status(Level.OFF, "No mailbox configured")
    polled = (
        f"Mailbox polled {_ago((now - state.last_success_at).total_seconds())}"
        if state.last_success_at
        else "Mailbox not polled yet"
    )
    if circuit == "open":
        wait = f"; next try in {round(circuit_retry_after)} s" if circuit_retry_after else ""
        return status(Level.WARNING, f"{polled} · provider paused after repeated failures{wait}")
    if state.consecutive_failures:
        return status(Level.WARNING, f"{polled} · last attempt failed")
    stale = state.last_success_at is None or (now - state.last_success_at).total_seconds() > (
        STALE_AFTER_INTERVALS * (interval or 60)
    )
    return status(Level.WARNING if stale and state.last_poll_at else Level.OK, polled)
