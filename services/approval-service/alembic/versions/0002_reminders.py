"""reminders for waiting gates (T2: every 15 min)

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from approval_service.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column(
        "approvals", sa.Column("reminders", sa.Integer, nullable=False, server_default="0"), schema=SCHEMA
    )
    op.add_column("approvals", sa.Column("reminded_at", sa.DateTime(timezone=True)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("approvals", "reminded_at", schema=SCHEMA)
    op.drop_column("approvals", "reminders", schema=SCHEMA)
