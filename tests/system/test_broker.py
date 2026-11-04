"""Outbox, relay and consumer on real RabbitMQ and Postgres: order, retry, DLQ, broker outage, one trace."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import aio_pika
import pytest
from conftest import POSTGRES_HOST, RABBITMQ, Proxy
from opspilot_common.events import EventEnvelope, Outbox, OutboxRelay
from opspilot_common.events.rabbitmq import (
    RabbitConnection,
    RabbitConsumer,
    RabbitPublisher,
    TransientConsumer,
)
from opspilot_common.resilience import ResilienceError, ResilienceRegistry, ResilienceSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def _registry() -> ResilienceRegistry:
    settings = ResilienceSettings.from_mapping(
        {
            "dependencies": {
                "rabbitmq": {
                    "timeout": 1,
                    "retry": {"attempts": 1},
                    "breaker": {"fail_max": 2, "reset_timeout": 0.2},
                }
            }
        }
    )
    return ResilienceRegistry(settings, listeners=())


@pytest.fixture
async def connection() -> AsyncIterator[RabbitConnection]:
    opened = RabbitConnection(RABBITMQ)
    await opened.open()
    yield opened
    await opened.close()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(f"postgresql+asyncpg://mail_svc:mail_dev_password@{POSTGRES_HOST}")
    yield created
    await created.dispose()


async def _wait(condition, seconds: float = 15.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.02)


async def _parked(connection: RabbitConnection, queue: str) -> bool:
    declared = await connection.channel.declare_queue(queue, durable=True, passive=True)
    return await declared.get(no_ack=True, fail=False) is not None


async def test_an_event_reaches_a_consumer_with_its_ids_and_trace(connection: RabbitConnection) -> None:
    kind, received = f"system.{uuid.uuid4().hex}", []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)

    await RabbitConsumer(connection, f"system.{kind}", [kind], handler).start()
    sent = EventEnvelope(
        type=kind,
        source="test",
        correlation_id="abc",
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    )
    await RabbitPublisher(connection, _registry().get("rabbitmq")).publish(sent)
    await _wait(lambda: received)
    assert received[0].id == sent.id
    assert received[0].traceparent == sent.traceparent
    assert received[0].correlation_id == "abc"


async def test_a_failing_handler_retries_and_is_then_parked(connection: RabbitConnection) -> None:
    kind, attempts = f"system.{uuid.uuid4().hex}", []

    async def handler(envelope: EventEnvelope) -> None:
        attempts.append(envelope.id)
        raise RuntimeError("always fails")

    queue = f"system.{kind}"
    consumer = RabbitConsumer(connection, queue, [kind], handler, max_retries=3, retry_delay_ms=50)
    await consumer.start()
    await RabbitPublisher(connection, _registry().get("rabbitmq")).publish(
        EventEnvelope(type=kind, source="test")
    )
    await _wait(lambda: len(attempts) >= 3)
    await asyncio.sleep(0.3)
    assert len(attempts) == 3
    assert await _parked(connection, f"{queue}.dlq")


async def test_an_unreadable_message_goes_straight_to_the_dlq(connection: RabbitConnection) -> None:
    kind, calls = f"system.{uuid.uuid4().hex}", []

    async def handler(envelope: EventEnvelope) -> None:
        calls.append(envelope)

    queue = f"system.{kind}"
    await RabbitConsumer(connection, queue, [kind], handler).start()
    await connection.exchange.publish(aio_pika.Message(body=b"not json"), routing_key=kind)
    await asyncio.sleep(0.3)
    assert not calls
    assert await _parked(connection, f"{queue}.dlq")


async def test_events_wait_in_the_outbox_during_a_broker_outage_and_none_is_lost(
    engine: AsyncEngine, rabbit_proxy: Proxy, connection: RabbitConnection
) -> None:
    from mail_service.adapters.outbound.sql import MailTables

    tables = MailTables(schema="mail")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    kind, received = f"system.{uuid.uuid4().hex}", []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope.id)

    await RabbitConsumer(connection, f"system.{kind}", [kind], handler).start()

    proxied = RabbitConnection(f"amqp://opspilot:opspilot@localhost:{rabbit_proxy.port}/")
    await proxied.open()
    relay = OutboxRelay(
        sessions, tables.outbox, RabbitPublisher(proxied, _registry().get("rabbitmq")), batch_size=10
    )
    envelopes = [EventEnvelope(type=kind, source="test") for _ in range(5)]
    ids = {e.id for e in envelopes}
    async with sessions() as session, session.begin():
        for envelope in envelopes:
            await Outbox(tables.outbox).add(session, envelope)

    async def pending() -> set[str]:
        async with sessions() as session:
            rows = await session.execute(
                select(tables.outbox.c.id).where(
                    tables.outbox.c.published_at.is_(None), tables.outbox.c.id.in_(ids)
                )
            )
            return {r.id for r in rows}

    await rabbit_proxy.down()
    for _ in range(3):
        with contextlib.suppress(ResilienceError):
            await relay.run_once()
        await asyncio.sleep(0.1)
    assert await pending() == ids  # nothing marked as published

    await rabbit_proxy.up()
    async with asyncio.timeout(30):
        while await pending():
            await relay.run_once()
            await asyncio.sleep(0.2)
    await _wait(lambda: ids <= set(received))
    assert sorted(received) == sorted(ids)  # all of them, once each, in the consumer
    await proxied.close()


async def test_a_live_queue_replaces_the_old_durable_one_and_goes_away_with_its_process(
    connection: RabbitConnection,
) -> None:
    kind, received = f"system.{uuid.uuid4().hex}", []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope.id)

    old = f"system.old.{uuid.uuid4().hex}"
    await connection.channel.declare_queue(old, durable=True)
    live = RabbitConnection(RABBITMQ)
    await live.open()
    await TransientConsumer(live, "system.live", [kind], handler, replaces=old).start()
    sent = EventEnvelope(type=kind, source="test")
    await RabbitPublisher(connection, _registry().get("rabbitmq")).publish(sent)
    await _wait(lambda: received)
    assert received == [sent.id]
    with pytest.raises(aio_pika.exceptions.ChannelNotFoundEntity):
        check = RabbitConnection(RABBITMQ)
        await check.open()
        try:
            await check.channel.declare_queue(old, passive=True)
        finally:
            await check.close()
    await live.close()
