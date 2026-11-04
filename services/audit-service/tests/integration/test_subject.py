from __future__ import annotations

from datetime import UTC, datetime

from audit_service.adapters.outbound.sql import AuditTables
from audit_service.adapters.outbound.subject import SqlAuditSubjectReader
from opspilot_common.subjects import SubjectQuery
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


async def test_finds_entries_by_token_exactly_and_has_no_way_to_change_them() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = AuditTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    base = {
        "occurred_at": NOW,
        "recorded_at": NOW,
        "kind": "privacy",
        "actor": "a",
        "event": "e",
        "privacy": "p",
        "event_type": "x",
        "source": "s",
    }
    rows = [
        {**base, "id": "1", "detail": "Viewed [PERSON_1]", "payload": {}},
        {**base, "id": "2", "detail": "n", "payload": {"subject_token": "PERSON_1"}},
        {**base, "id": "3", "detail": "Viewed [PERSON_11]", "payload": {"subject_token": "PERSON_11"}},
        {**base, "id": "4", "detail": "n", "payload": {"run_id": "r-1"}},
    ]
    async with sessions() as s, s.begin():
        await s.execute(insert(tables.entries), rows)
    reader = SqlAuditSubjectReader(sessions, tables)
    assert (await reader.report(SubjectQuery(tokens=["PERSON_1"]))).count == 2
    report = await reader.report(SubjectQuery(tokens=["PERSON_1"], run_ids=["r-1"], include_items=True))
    assert report.count == 3 and report.erase_action == "Keep, token only" and len(report.items) == 3
    assert not hasattr(reader, "erase")
    await engine.dispose()
