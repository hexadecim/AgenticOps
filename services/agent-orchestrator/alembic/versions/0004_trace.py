"""W3C trace context on queued work, so a case stays one trace across workers

Revision ID: 0004
"""

import sqlalchemy as sa
from agent_orchestrator.config import Settings
from alembic import op

revision = "0004"
down_revision = "0003"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("runs", sa.Column("trace", sa.String(64)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("runs", "trace", schema=SCHEMA)
