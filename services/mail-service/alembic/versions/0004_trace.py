"""W3C trace context on queued work, so a case stays one trace across workers

Revision ID: 0004
"""

import sqlalchemy as sa
from alembic import op
from mail_service.config import Settings

revision = "0004"
down_revision = "0003"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("outgoing", sa.Column("trace", sa.String(64)), schema=SCHEMA)
    op.add_column("dsr_messages", sa.Column("trace", sa.String(64)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("outgoing", "trace", schema=SCHEMA)
    op.drop_column("dsr_messages", "trace", schema=SCHEMA)
