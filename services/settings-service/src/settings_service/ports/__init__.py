"""Ports of settings-service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from opspilot_common.events.envelope import EventPayload

from ..domain.document import SettingsDocument


class SettingsRepository(Protocol):
    async def get(self, section: str) -> SettingsDocument | None: ...
    async def save(self, document: SettingsDocument, *, expected_version: int) -> None:
        """Persist; raise VersionConflictError when the stored version is not `expected_version`."""
        ...


class EventRecorder(Protocol):
    def record(self, payload: EventPayload, *, actor: str | None = None) -> None: ...


class UnitOfWork(Protocol):
    settings: SettingsRepository
    events: EventRecorder

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ConnectionReport:
    ok: bool
    summary: str
    checks: list[Check] = field(default_factory=list)
    duration_ms: int = 0

    @classmethod
    def from_checks(cls, checks: list[Check], ok_summary: str, duration_ms: int = 0) -> ConnectionReport:
        failed = next((c for c in checks if not c.ok), None)
        return cls(
            ok=failed is None,
            summary=ok_summary if failed is None else failed.detail,
            checks=checks,
            duration_ms=duration_ms,
        )


class ConnectionTester(Protocol):
    """Checks that a candidate configuration actually works. One implementation per provider/system."""

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
