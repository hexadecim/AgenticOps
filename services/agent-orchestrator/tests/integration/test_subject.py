"""A person's runs and checkpoints: erased for good, with other people's runs and a busy run left alone."""

from __future__ import annotations

from datetime import UTC, datetime

import opspilot_common.events.catalogue as c
import pytest
from agent_orchestrator.adapters.outbound.sql import OrchestratorTables
from agent_orchestrator.adapters.outbound.subject import SqlRunSubjectStore
from agent_orchestrator.domain.errors import RunInProgressError
from opspilot_common.subjects import SubjectQuery
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


class Saver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def _request() -> c.SubjectErasureRequested:
    return c.SubjectErasureRequested(
        dsr_id="DSR-0001",
        tokens=["EMAIL_1"],
        own_email_ids=["e-1"],
        run_ids=["r-1"],
        decided_by_role="compliance_lead",
    )


async def _world(status: str = "resolved"):  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = OrchestratorTables(schema=None)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(tables.metadata.create_all)
    base = {"thread_id": "t", "created_at": NOW, "updated_at": NOW, "due_at": NOW, "attempts": 0}
    async with sessions() as s, s.begin():
        await s.execute(
            insert(tables.runs),
            [
                {**base, "id": "r-1", "email_id": "e-1", "status": status},
                {**base, "id": "r-2", "email_id": "e-2", "status": "queued"},
            ],
        )
    store = SqlRunSubjectStore(sessions, tables)
    saver = Saver()
    store.use_checkpointer(saver)
    return engine, store, sessions, tables, saver


@pytest.mark.parametrize(
    ("status", "open_runs", "busy"),
    [("resolved", 0, 0), ("waiting_approval", 1, 0), ("queued", 1, 0), ("running", 1, 1)],
)
async def test_report_says_which_runs_are_open_or_busy(status: str, open_runs: int, busy: int) -> None:
    engine, store, *_ = await _world(status)
    report = await store.report(SubjectQuery(own_email_ids=["e-1"]))
    assert (report.run_ids, report.open_runs, report.busy_runs) == (["r-1"], open_runs, busy)
    await engine.dispose()


async def test_erase_removes_their_run_and_checkpoints_only() -> None:
    engine, store, sessions, tables, saver = await _world()
    await store.erase(_request())
    async with sessions() as s:
        left = {r.id for r in await s.execute(select(tables.runs.c.id))}
        acks = [r.type for r in await s.execute(select(tables.outbox.c.type))]
    assert left == {"r-2"} and saver.deleted == ["r-1"] and acks == ["subject.store_erased"]
    await engine.dispose()


async def test_a_run_in_progress_makes_the_event_come_back_later() -> None:
    engine, store, sessions, tables, saver = await _world("running")
    with pytest.raises(RunInProgressError):
        await store.erase(_request())
    async with sessions() as s:
        assert len(list(await s.execute(select(tables.runs)))) == 2
    assert saver.deleted == []
    await engine.dispose()
