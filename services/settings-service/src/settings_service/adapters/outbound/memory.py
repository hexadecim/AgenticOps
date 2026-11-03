"""In-memory adapters for unit tests."""

from __future__ import annotations

import copy
from types import TracebackType
from typing import Self

from opspilot_common.events.envelope import EventPayload

from ...domain.document import SettingsDocument
from ...domain.errors import VersionConflictError


class InMemorySettingsRepository:
    def __init__(self) -> None:
        self.rows: dict[str, SettingsDocument] = {}

    async def get(self, section: str) -> SettingsDocument | None:
        return copy.deepcopy(self.rows.get(section))

    async def save(self, document: SettingsDocument, *, expected_version: int) -> None:
        current = self.rows.get(document.section)
        if (current.version if current else 0) != expected_version:
            raise VersionConflictError()
        self.rows[document.section] = copy.deepcopy(document)


class InMemoryUnitOfWork:
    def __init__(
        self, store: InMemorySettingsRepository, published: list[tuple[EventPayload, str | None]]
    ) -> None:
        self._store, self._published = store, published

    async def __aenter__(self) -> Self:
        self.settings = InMemorySettingsRepository()
        self.settings.rows = copy.deepcopy(self._store.rows)
        self._pending: list[tuple[EventPayload, str | None]] = []
        self.events = self
        return self

    def record(self, payload: EventPayload, *, actor: str | None = None) -> None:
        self._pending.append((payload, actor))

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._pending.clear()

    async def commit(self) -> None:
        self._store.rows = copy.deepcopy(self.settings.rows)
        self._published.extend(self._pending)
        self._pending.clear()
