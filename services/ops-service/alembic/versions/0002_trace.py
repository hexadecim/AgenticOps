"""W3C trace context on queued work, so a case stays one trace across workers

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from ops_service.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("fix_jobs", sa.Column("trace", sa.String(64)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("fix_jobs", "trace", schema=SCHEMA)
