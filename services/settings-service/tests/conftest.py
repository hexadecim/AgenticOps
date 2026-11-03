from __future__ import annotations

from datetime import UTC, datetime

import pytest
from settings_service.adapters.outbound.memory import InMemorySettingsRepository, InMemoryUnitOfWork


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


class World:
    def __init__(self) -> None:
        self.store = InMemorySettingsRepository()
        self.events: list = []  # type: ignore[type-arg]

    def uow(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store, self.events)


@pytest.fixture
def world() -> World:
    return World()


IMAP_VALUES = {
    "provider": "imap",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "username": "support@example.com",
    "folder": "INBOX",
}
