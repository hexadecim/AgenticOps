"""Pytest plugin: runs a service's SQLite-based tests on real Postgres, unchanged.

Every engine a test asks for with a sqlite URL gets a new schema instead, first on its search_path, so `schema=""`
tables land there. Loaded by `make test-postgres` with `-p postgres_plugin`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import sqlalchemy
import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy import event

POSTGRES = os.environ.get("TEST_POSTGRES", "postgresql+asyncpg://opspilot:opspilot@localhost:55432/opspilot")
_real = sa_asyncio.create_async_engine
_schemas: list[str] = []


def _create_async_engine(url: Any, **kwargs: Any) -> sa_asyncio.AsyncEngine:
    if not str(url).startswith("sqlite"):
        return _real(url, **kwargs)
    schema = f"t_{uuid.uuid4().hex}"
    _schemas.append(schema)
    engine = _real(POSTGRES, connect_args={"server_settings": {"search_path": f"{schema},public"}})

    @event.listens_for(engine.sync_engine, "connect")
    def create_schema(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cursor.close()

    return engine


def pytest_configure() -> None:
    sa_asyncio.create_async_engine = _create_async_engine


def pytest_sessionfinish() -> None:
    engine = sqlalchemy.create_engine(POSTGRES.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        for schema in _schemas:
            connection.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def pytest_terminal_summary(terminalreporter: Any) -> None:
    terminalreporter.write_line(f"postgres_plugin: {len(_schemas)} engines redirected to Postgres")
