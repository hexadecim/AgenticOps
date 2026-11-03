"""In-memory audit log for unit tests (same contract as SQL: append-only, keyset paging)."""

from __future__ import annotations

from ...domain.entry import AuditEntry, AuditPage, AuditQuery


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.entries: dict[str, AuditEntry] = {}

    async def append(self, entry: AuditEntry) -> bool:
        if entry.id in self.entries:
            return False
        self.entries[entry.id] = entry
        return True

    async def search(self, query: AuditQuery) -> AuditPage:
        rows = sorted(self.entries.values(), key=lambda e: (e.occurred_at, e.id), reverse=True)
        if query.kind:
            rows = [e for e in rows if e.kind is query.kind]
        if query.text:
            needle = query.text.lower()
            rows = [
                e
                for e in rows
                if needle
                in " ".join(filter(None, (e.actor, e.event, e.detail, e.ticket, e.thread_id))).lower()
            ]
        if query.before:
            rows = [e for e in rows if (e.occurred_at, e.id) < query.before]
        page = rows[: query.limit]
        more = len(rows) > query.limit
        return AuditPage(page, (page[-1].occurred_at, page[-1].id) if more and page else None)
