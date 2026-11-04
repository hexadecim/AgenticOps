from __future__ import annotations

from datetime import UTC, datetime

import opspilot_common.events.catalogue as c
from opspilot_common.subjects import SubjectQuery
from reporting_service.adapters.outbound.sql import ReportingTables
from reporting_service.adapters.outbound.subject import SqlReportingSubjectStore
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


async def test_their_cases_and_notes_go_and_others_stay() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = ReportingTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    case = {"thread_id": "t", "status": "resolved", "paused": False, "title": "[PERSON_1] upload"}
    note = {"at": NOW, "actor": "a", "text": "n", "system": False}
    async with sessions() as s, s.begin():
        await s.execute(insert(tables.cases), [{**case, "run_id": "r-1"}, {**case, "run_id": "r-2"}])
        await s.execute(
            insert(tables.timeline),
            [{**note, "id": "n1", "run_id": "r-1"}, {**note, "id": "n2", "run_id": "r-2"}],
        )
        await s.execute(insert(tables.emails), {"email_id": "e-1", "received_at": NOW})
    store = SqlReportingSubjectStore(sessions, tables)
    assert (await store.report(SubjectQuery(run_ids=["r-1"]))).count == 2
    await store.erase(
        c.SubjectErasureRequested(
            dsr_id="DSR-0001",
            tokens=[],
            own_email_ids=["e-1"],
            run_ids=["r-1"],
            decided_by_role="compliance_lead",
        )
    )
    async with sessions() as s:
        assert {r.run_id for r in await s.execute(select(tables.cases.c.run_id))} == {"r-2"}
        assert {r.id for r in await s.execute(select(tables.timeline.c.id))} == {"n2"}
        assert list(await s.execute(select(tables.emails))) == []
        assert [r.type for r in await s.execute(select(tables.outbox.c.type))] == ["subject.store_erased"]
    await engine.dispose()
