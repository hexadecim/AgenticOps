from datetime import UTC, datetime

import pytest
from opspilot_common.events import (
    EventEnvelope,
    EventType,
    Inbox,
    InMemoryEventBus,
    Outbox,
    OutboxRelay,
    inbox_table,
    outbox_table,
    parse_payload,
    topic_matches,
)
from opspilot_common.events.catalogue import ApprovalDecided, Decision, EmailReceived, Gate
from opspilot_common.resilience import CircuitOpenError
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def email_event() -> EventEnvelope:
    payload = EmailReceived(email_id="e-1", thread_id="t-1", provider="imap", received_at=datetime.now(UTC))
    return EventEnvelope.wrap(payload, source="mail-service", correlation_id="c-1")


def test_wrap_and_parse_round_trip() -> None:
    env = email_event()
    assert env.type == EventType.EMAIL_RECEIVED
    again = EventEnvelope.from_bytes(env.to_bytes())
    parsed = parse_payload(again)
    assert isinstance(parsed, EmailReceived)
    assert parsed.email_id == "e-1"


def test_causation_carries_correlation() -> None:
    cause = email_event()
    decided = ApprovalDecided(
        approval_id="a",
        run_id="r",
        thread_id="t",
        gate=Gate.TICKET,
        decision=Decision.APPROVE,
        decided_by="u1",
        decided_by_role="l1_analyst",
    )
    env = EventEnvelope.wrap(decided, source="approval-service", causation=cause)
    assert env.correlation_id == "c-1"
    assert env.causation_id == cause.id


@pytest.mark.parametrize(
    ("pattern", "key", "expected"),
    [
        ("#", "approval.decided", True),
        ("approval.*", "approval.decided", True),
        ("approval.*", "jira.issue.created", False),
        ("jira.#", "jira.issue.created", True),
        ("*.created", "jira.issue.created", False),
        ("email.received", "email.received", True),
    ],
)
def test_topic_matching(pattern: str, key: str, expected: bool) -> None:
    assert topic_matches(pattern, key) is expected


@pytest.fixture
async def db():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    tables = outbox_table(metadata), inbox_table(metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False), tables
    await engine.dispose()


async def test_outbox_relay_publishes_in_order_and_once(db) -> None:  # type: ignore[no-untyped-def]
    sessions, (outbox_t, _) = db
    outbox, bus = Outbox(outbox_t), InMemoryEventBus()
    events = [email_event(), email_event()]
    async with sessions() as s, s.begin():
        for e in events:
            await outbox.add(s, e)
    relay = OutboxRelay(sessions, outbox_t, bus)
    assert await relay.run_once() == 2
    assert await relay.run_once() == 0
    assert [e.id for e in bus.published] == [e.id for e in events]


async def test_outbox_relay_defers_when_bus_circuit_open(db) -> None:  # type: ignore[no-untyped-def]
    sessions, (outbox_t, _) = db

    class OpenBus:
        async def publish(self, envelope: EventEnvelope) -> None:
            raise CircuitOpenError("rabbitmq", 10)

    async with sessions() as s, s.begin():
        await Outbox(outbox_t).add(s, email_event())
    assert await OutboxRelay(sessions, outbox_t, OpenBus()).run_once() == 0
    async with sessions() as s:
        row = (await s.execute(select(outbox_t))).one()
    assert row.published_at is None
    assert row.attempts == 1
    assert "circuit open" in row.last_error


async def test_inbox_guard_processes_redelivery_once(db) -> None:  # type: ignore[no-untyped-def]
    sessions, (_, inbox_t) = db
    seen: list[str] = []

    async def handler(session, envelope: EventEnvelope) -> None:  # type: ignore[no-untyped-def]
        seen.append(envelope.id)

    guarded = Inbox(inbox_t, "audit").guard(sessions, handler)
    env = email_event()
    await guarded(env)
    await guarded(env)
    assert seen == [env.id]
