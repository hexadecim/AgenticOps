"""the run's reporter (tokens and email id), so a resolution can reach everyone linked to a ticket

Revision ID: 0003
"""

import sqlalchemy as sa
from agent_orchestrator.config import Settings
from alembic import op

revision = "0003"
down_revision = "0002"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("runs", sa.Column("reporter", sa.JSON), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("runs", "reporter", schema=SCHEMA)
