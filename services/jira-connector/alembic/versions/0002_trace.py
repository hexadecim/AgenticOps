"""W3C trace context on queued work, so a case stays one trace across workers

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from jira_connector.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("jobs", sa.Column("trace", sa.String(64)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("jobs", "trace", schema=SCHEMA)
