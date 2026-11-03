"""Ports of audit-service: writing and reading are separate, and there is no update or delete operation."""

from __future__ import annotations

from typing import Protocol

from ..domain.entry import AuditEntry, AuditPage, AuditQuery


class AuditWriter(Protocol):
    async def append(self, entry: AuditEntry) -> bool:
        """Store the entry; False when an entry with this id already exists (redelivered event)."""
        ...


class AuditReader(Protocol):
    async def search(self, query: AuditQuery) -> AuditPage: ...
