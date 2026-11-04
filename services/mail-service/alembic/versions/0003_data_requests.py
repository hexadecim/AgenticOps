"""emails answering data requests, and tombstones of erased emails

Revision ID: 0003
"""

import sqlalchemy as sa
from alembic import op
from mail_service.config import Settings

revision = "0003"
down_revision = "0002"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "dsr_messages",
        sa.Column("id", sa.String(60), primary_key=True),
        sa.Column("dsr_id", sa.String(12), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, index=True),
        sa.Column("tries", sa.Integer, nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_try_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "erased_emails",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("erased_emails", schema=SCHEMA)
    op.drop_table("dsr_messages", schema=SCHEMA)
