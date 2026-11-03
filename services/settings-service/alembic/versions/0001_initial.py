"""sections, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from settings_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "sections",
        sa.Column("section", sa.String(40), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("values", sa.JSON, nullable=False),
        sa.Column("secrets_enc", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.String(80)),
        schema=SCHEMA,
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(80), nullable=False, index=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("published_at", sa.DateTime(timezone=True), index=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        schema=SCHEMA,
    )
    op.create_table(
        "inbox",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("consumer", sa.String(80), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in ("inbox", "outbox", "sections"):
        op.drop_table(table, schema=SCHEMA)
