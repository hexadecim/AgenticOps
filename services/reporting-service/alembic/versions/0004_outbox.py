"""outbox: reporting-service now acknowledges erasures

Revision ID: 0004
"""

import sqlalchemy as sa
from alembic import op
from reporting_service.config import Settings

revision = "0004"
down_revision = "0003"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(80), nullable=False, index=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("outbox", schema=SCHEMA)
