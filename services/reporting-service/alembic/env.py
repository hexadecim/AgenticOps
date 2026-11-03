"""Alembic env: async engine, migrations and version table live in the service's own schema."""

import asyncio

from alembic import context
from reporting_service.config import Settings
from sqlalchemy.ext.asyncio import create_async_engine

settings = Settings()  # type: ignore[call-arg]
SCHEMA = settings.db_schema


def run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection, target_metadata=None, version_table_schema=SCHEMA, include_schemas=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        await conn.run_sync(run)
        await conn.commit()
    await engine.dispose()


asyncio.run(main())
