"""Milestone 0 live demo: exercises opspilot-common against the real infrastructure.

Run after `make infra-up` (Ollama must be running on the host):
    uv run python scripts/demo_foundations.py

1. Outbox (Postgres, `mail` schema) → relay → RabbitMQ → consumer with inbox dedupe
2. Poison message → retry queue → parked in DLQ
3. Circuit breaker on Ollama via toxiproxy: closed → open (proxy cut) → half-open → closed (restored),
   with state shared in Redis
4. A real deepseek-r1:8b call through the resilient client, structured JSON output
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
from opspilot_common.events import EventEnvelope, Outbox, OutboxRelay, outbox_table
from opspilot_common.events.catalogue import EmailReceived
from opspilot_common.events.rabbitmq import RabbitConnection, RabbitConsumer, RabbitPublisher
from opspilot_common.logging import configure_logging
from opspilot_common.resilience import (
    CircuitOpenError,
    CircuitState,
    PrometheusListener,
    RedisStateStore,
    ResilienceError,
    ResilienceRegistry,
    ResilienceSettings,
    ResilientHttpClient,
    WallClock,
)
from redis.asyncio import Redis
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PG = "postgresql+asyncpg://mail_svc:mail_dev_password@localhost:5432/opspilot"
AMQP = "amqp://opspilot:opspilot@localhost:5672/"
TOXIPROXY = "http://localhost:8474"
OLLAMA_VIA_PROXY = "http://localhost:21434"

BOLD, GREEN, RED, AMBER, DIM, END = "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
COLOUR = {CircuitState.CLOSED: GREEN, CircuitState.HALF_OPEN: AMBER, CircuitState.OPEN: RED}


def step(title: str) -> None:
    print(f"\n{BOLD}▶ {title}{END}")


class PrintListener:
    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
        print(
            f"   {DIM}breaker[{dependency}]{END} {COLOUR[old]}{old.value}{END} → {COLOUR[new]}{new.value}{END}"
        )


def email_event(n: int) -> EventEnvelope:
    payload = EmailReceived(
        email_id=f"email-{n}", thread_id=f"thr-{n}", provider="imap", received_at=datetime.now(UTC)
    )
    return EventEnvelope.wrap(payload, source="mail-service", correlation_id=f"demo-{n}")


async def demo_outbox_and_bus(registry: ResilienceRegistry) -> None:
    step("1. Transactional outbox (Postgres) → relay → RabbitMQ → consumer")
    engine = create_async_engine(PG)
    metadata = MetaData()
    # Its own table: the demo must never touch mail-service's real outbox.
    table = outbox_table(MetaData(), schema="mail").to_metadata(metadata, name="demo_outbox")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(text("delete from mail.demo_outbox"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    connection = RabbitConnection(AMQP)
    await connection.open()
    received: list[str] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope.payload["email_id"])
        print(
            f"   consumer got {envelope.type} {envelope.payload['email_id']} (correlation {envelope.correlation_id})"
        )
        if len(received) == 3:
            done.set()

    await RabbitConsumer(connection, "demo.orchestrator", ["email.received"], handler).start()

    async with sessions() as s, s.begin():  # the state change and its events commit together
        for n in range(1, 4):
            await Outbox(table).add(s, email_event(n))
    print("   wrote 3 events to mail.demo_outbox in one transaction")
    publisher = RabbitPublisher(connection, registry.get("rabbitmq"))
    published = await OutboxRelay(sessions, table, publisher).run_once()
    print(
        f"   relay published {published}; second run published {await OutboxRelay(sessions, table, publisher).run_once()}"
    )
    await asyncio.wait_for(done.wait(), 10)
    print(f"   {GREEN}✓ delivered in order: {received}{END}")

    step("2. Poison message → retry queue → parked in DLQ")
    attempts = 0
    parked = asyncio.Event()

    async def always_fails(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        print(f"   attempt {attempts}: handler raises")
        if attempts == 3:
            asyncio.get_running_loop().call_later(0.5, parked.set)
        raise RuntimeError("cannot process")

    await RabbitConsumer(
        connection, "demo.poison", ["demo.poison"], always_fails, max_retries=3, retry_delay_ms=500
    ).start()
    await publisher.publish(EventEnvelope(type="demo.poison", source="demo", payload={}))
    await asyncio.wait_for(parked.wait(), 15)
    async with httpx.AsyncClient(auth=("opspilot", "opspilot")) as http:
        for _ in range(30):  # management stats refresh every few seconds
            q = (await http.get("http://localhost:15672/api/queues/%2F/demo.poison.dlq")).json()
            if q.get("messages"):
                break
            await asyncio.sleep(0.5)
    print(
        f"   {GREEN}✓ parked after {attempts} attempts; demo.poison.dlq holds {q.get('messages', '?')} message(s){END}"
    )
    await connection.close()
    await engine.dispose()


async def set_proxy(enabled: bool) -> None:
    async with httpx.AsyncClient() as http:
        await http.post(f"{TOXIPROXY}/proxies/ollama", json={"enabled": enabled})


async def demo_breaker(registry: ResilienceRegistry, redis: Redis) -> None:
    step("3. Circuit breaker on Ollama (via toxiproxy), state shared in Redis")
    ollama = ResilientHttpClient.from_registry(registry, "ollama", base_url=OLLAMA_VIA_PROXY)
    breaker = registry.get("ollama").breaker

    resp = await ollama.get("/api/tags")
    print(f"   healthy call: HTTP {resp.status_code}, models {[m['name'] for m in resp.json()['models']]}")

    await set_proxy(False)
    print(f"   {RED}toxiproxy: Ollama link cut{END}")
    for n in range(1, 5):
        t0 = time.perf_counter()
        try:
            await ollama.get("/api/tags")
        except CircuitOpenError as exc:
            print(
                f"   call {n}: {AMBER}short-circuited in {(time.perf_counter() - t0) * 1000:.1f} ms{END} "
                f"(retry after {exc.retry_after:.1f}s) — Ollama not contacted"
            )
        except ResilienceError as exc:
            print(
                f"   call {n}: failed after retries ({type(exc).__name__}) in {(time.perf_counter() - t0):.1f}s"
            )
        except httpx.TransportError as exc:
            print(
                f"   call {n}: failed after retries ({type(exc).__name__}) in {(time.perf_counter() - t0):.1f}s"
            )
    shared = json.loads(await redis.get("opspilot:breaker:ollama"))
    print(f"   Redis opspilot:breaker:ollama = {shared}  ← every replica sees this")

    await set_proxy(True)
    print(f"   {GREEN}toxiproxy: Ollama link restored{END}; waiting for reset timeout…")
    await asyncio.sleep(3.1)
    print(f"   state now: {(await breaker.state()).value}")
    resp = await ollama.get("/api/tags")
    print(f"   {GREEN}✓ probe call HTTP {resp.status_code}; circuit {(await breaker.state()).value}{END}")
    await ollama.aclose()


async def demo_llm(registry: ResilienceRegistry) -> None:
    step("4. deepseek-r1:8b through the resilient client (structured JSON output)")
    ollama = ResilientHttpClient.from_registry(registry, "ollama", base_url="http://localhost:11434")
    email = (
        "Subject: Upload stuck at 0%, telemetry_0925.zip\n"
        "Hello support, since 08:40 our upload from machine M-2210 is stuck at 0%. Retried three times. "
        "Regards, [PERSON_1]"
    )
    schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["upload failure", "login or access", "report not accessible", "other"],
            },
            "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3", "sev4"]},
            "machine": {"type": "string"},
            "should_create_incident": {"type": "boolean"},
        },
        "required": ["category", "severity", "machine", "should_create_incident"],
    }
    print(f"   {DIM}{email}{END}")
    t0 = time.perf_counter()
    resp = await ollama.post(
        "/api/chat",
        idempotency_key="demo-intake-1",
        json={
            "model": "deepseek-r1:8b",
            "stream": False,
            "think": True,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": "Classify the support email. Reply with JSON only."},
                {"role": "user", "content": email},
            ],
        },
    )
    msg = resp.json()["message"]
    print(f"   reasoning ({len(msg.get('thinking') or '')} chars, kept out of the result)")
    print(f"   {GREEN}✓ result in {time.perf_counter() - t0:.1f}s: {json.loads(msg['content'])}{END}")
    await ollama.aclose()


async def main() -> None:
    configure_logging("demo", level="CRITICAL")  # expected failures are narrated, not logged
    settings = ResilienceSettings.from_mapping(
        {
            "defaults": {"timeout": 5, "retry": {"attempts": 2, "base_delay": 0.1, "max_delay": 0.2}},
            "dependencies": {
                "ollama": {
                    "timeout": 300,
                    "bulkhead": 2,
                    "bulkhead_wait": 600,
                    "breaker": {"fail_max": 2, "reset_timeout": 3},
                },
                "rabbitmq": {"timeout": 5},
            },
        }
    )
    redis = Redis.from_url("redis://localhost:6379/0")
    await redis.delete("opspilot:breaker:ollama")
    registry = ResilienceRegistry(
        settings,
        store=RedisStateStore(redis),
        clock=WallClock(),
        listeners=[PrintListener(), PrometheusListener()],
    )
    await demo_outbox_and_bus(registry)
    await demo_breaker(registry, redis)
    await demo_llm(registry)
    await redis.aclose()
    print(f"\n{BOLD}{GREEN}All foundation demos passed.{END}")


if __name__ == "__main__":
    asyncio.run(main())
