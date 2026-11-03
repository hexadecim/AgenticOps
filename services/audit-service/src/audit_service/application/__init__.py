"""audit-service use cases: record every event, search the log."""

from __future__ import annotations

from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.events.envelope import EventEnvelope

from ..domain.entry import AuditPage, AuditQuery
from ..domain.mapping import describe
from ..ports import AuditReader, AuditWriter

# Who may read the audit log (the wireframe shows it to the Compliance lead).
AUDIT_READERS = frozenset({Role.COMPLIANCE_LEAD, Role.SUPPORT_MANAGER, Role.ADMIN})
MAX_PAGE = 200


class NotAllowedError(Exception):
    pass


class RecordEvent:
    async def __call__(self, envelope: EventEnvelope, log: AuditWriter) -> bool:
        return await log.append(describe(envelope))


class SearchAudit:
    def __init__(self, reader: AuditReader) -> None:
        self._reader = reader

    async def __call__(self, principal: Principal, query: AuditQuery) -> AuditPage:
        if not principal.has_any(*AUDIT_READERS):
            raise NotAllowedError()
        limit = max(1, min(query.limit, MAX_PAGE))
        text = (query.text or "").strip() or None
        return await self._reader.search(AuditQuery(query.kind, text, query.before, limit))
