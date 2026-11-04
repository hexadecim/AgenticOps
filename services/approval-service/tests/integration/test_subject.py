from __future__ import annotations

from datetime import UTC, datetime

import opspilot_common.events.catalogue as c
from approval_service.adapters.outbound.sql import ApprovalTables
from approval_service.adapters.outbound.subject import SqlApprovalSubjectStore
from opspilot_common.subjects import SubjectQuery
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


async def test_approvals_on_their_runs_go_and_others_stay() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = ApprovalTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    base = {
        "thread_id": "t",
        "gate": "ticket",
        "status": "pending",
        "title": "[PERSON_1] cannot upload",
        "detail": "d",
        "proposal": {},
        "requested_at": NOW,
        "edits": [],
        "reminders": 0,
    }
    async with sessions() as s, s.begin():
        await s.execute(
            insert(tables.approvals),
            [{**base, "id": "a", "run_id": "r-1"}, {**base, "id": "b", "run_id": "r-2"}],
        )
    store = SqlApprovalSubjectStore(sessions, tables)
    report = await store.report(SubjectQuery(run_ids=["r-1"], include_items=True))
    assert report.count == 1 and report.items[0]["title"] == "[PERSON_1] cannot upload"
    assert (await store.report(SubjectQuery(run_ids=["r-1"]))).items == []
    await store.erase(
        c.SubjectErasureRequested(
            dsr_id="DSR-0001",
            tokens=["PERSON_1"],
            own_email_ids=[],
            run_ids=["r-1"],
            decided_by_role="compliance_lead",
        )
    )
    async with sessions() as s:
        assert {r.id for r in await s.execute(select(tables.approvals.c.id))} == {"b"}
        assert [r.type for r in await s.execute(select(tables.outbox.c.type))] == ["subject.store_erased"]
    await engine.dispose()
