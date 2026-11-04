"""Fixtures for tests against real infrastructure (deploy/docker-compose.test.yml)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

ROOT = Path(__file__).parents[2]
POSTGRES = os.environ.get("TEST_POSTGRES", "postgresql+asyncpg://opspilot:opspilot@localhost:55432/opspilot")
POSTGRES_HOST = POSTGRES.split("@")[1]
RABBITMQ = os.environ.get("TEST_RABBITMQ", "amqp://opspilot:opspilot@localhost:55672/")
REDIS = os.environ.get("TEST_REDIS", "redis://localhost:56379/0")
TOXIPROXY = os.environ.get("TEST_TOXIPROXY", "http://localhost:58474")


class Proxy:
    """One Toxiproxy proxy: `down()` refuses connections, `slow()` holds every response."""

    def __init__(self, api: httpx.AsyncClient, name: str, port: int) -> None:
        self._api, self.name, self.port = api, name, port

    async def _enabled(self, enabled: bool) -> None:
        (await self._api.post(f"/proxies/{self.name}", json={"enabled": enabled})).raise_for_status()

    async def down(self) -> None:
        await self._enabled(False)

    async def up(self) -> None:
        await self._enabled(True)

    async def slow(self, milliseconds: int) -> None:
        toxic = {"name": "slow", "type": "latency", "attributes": {"latency": milliseconds}}
        (await self._api.post(f"/proxies/{self.name}/toxics", json=toxic)).raise_for_status()

    async def normal(self) -> None:
        await self._api.delete(f"/proxies/{self.name}/toxics/slow")
        await self.up()


@pytest.fixture
async def toxiproxy() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=TOXIPROXY) as client:
        (await client.post("/reset")).raise_for_status()
        yield client


async def _proxy(api: httpx.AsyncClient, name: str, port: int, upstream: str) -> Proxy:
    await api.delete(f"/proxies/{name}")
    body = {"name": name, "listen": f"0.0.0.0:{port}", "upstream": upstream, "enabled": True}
    (await api.post("/proxies", json=body)).raise_for_status()
    return Proxy(api, name, port)


@pytest.fixture
async def http_proxy(toxiproxy: httpx.AsyncClient) -> Proxy:
    return await _proxy(toxiproxy, "http", 58501, "upstream:80")


@pytest.fixture
async def rabbit_proxy(toxiproxy: httpx.AsyncClient) -> Proxy:
    return await _proxy(toxiproxy, "rabbitmq", 58502, "rabbitmq:5672")


@pytest.fixture
async def redis_proxy(toxiproxy: httpx.AsyncClient) -> Proxy:
    return await _proxy(toxiproxy, "redis", 58503, "redis:6379")


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS)
    await client.flushdb()
    yield client
    await client.aclose()
