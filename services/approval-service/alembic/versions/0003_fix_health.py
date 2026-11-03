"""health re-check result on approved fixes (T2)

Revision ID: 0003
"""

import sqlalchemy as sa
from alembic import op
from approval_service.config import Settings

revision = "0003"
down_revision = "0002"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("approvals", sa.Column("health", sa.String(12)), schema=SCHEMA)
    op.add_column("approvals", sa.Column("health_detail", sa.Text), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("approvals", "health_detail", schema=SCHEMA)
    op.drop_column("approvals", "health", schema=SCHEMA)
