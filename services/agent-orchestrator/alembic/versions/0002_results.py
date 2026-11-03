"""results of other services kept on the run (ticket, fix) until the graph waits for them

Revision ID: 0002
"""

import sqlalchemy as sa
from agent_orchestrator.config import Settings
from alembic import op

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("runs", sa.Column("results", sa.JSON), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("runs", "results", schema=SCHEMA)
