from __future__ import annotations

from datetime import UTC, datetime

import opspilot_common.events.catalogue as c
from jira_connector.adapters.outbound.sql import JiraTables
from jira_connector.adapters.outbound.subject import SqlJiraSubjectStore
from opspilot_common.subjects import SubjectQuery
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


async def test_the_link_to_their_ticket_goes_and_the_answer_says_the_ticket_stays() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = JiraTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    job = {
        "thread_id": "t",
        "action": "create",
        "ticket": {},
        "status": "done",
        "attempts": 1,
        "issue_key": "KAN-1",
        "created_at": NOW,
        "next_attempt_at": NOW,
    }
    async with sessions() as s, s.begin():
        await s.execute(
            insert(tables.jobs), [{**job, "id": "a", "run_id": "r-1"}, {**job, "id": "b", "run_id": "r-2"}]
        )
        await s.execute(
            insert(tables.proposals),
            {"approval_id": "a", "run_id": "r-1", "thread_id": "t", "action": "create", "ticket": {}},
        )
    store = SqlJiraSubjectStore(sessions, tables)
    report = await store.report(SubjectQuery(run_ids=["r-1"], include_items=True))
    assert (
        report.count == 1 and report.items[0]["ticket"] == "KAN-1" and report.erase_action == "Unlink token"
    )
    await store.erase(
        c.SubjectErasureRequested(
            dsr_id="DSR-0001",
            tokens=["EMAIL_1"],
            own_email_ids=[],
            run_ids=["r-1"],
            decided_by_role="compliance_lead",
        )
    )
    async with sessions() as s:
        assert {r.id for r in await s.execute(select(tables.jobs.c.id))} == {"b"}
        assert list(await s.execute(select(tables.proposals))) == []
        ack = (await s.execute(select(tables.outbox.c.body))).scalar_one()
    assert "stay in Jira" in ack
    await engine.dispose()
