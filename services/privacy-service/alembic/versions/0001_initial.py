"""vault, token_counters, pseudonymisations, reveals, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from privacy_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "vault",
        sa.Column("token", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("value_enc", sa.Text, nullable=False),
        sa.Column("subject", sa.String(40), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "lookup_hash", name="uq_vault_kind_lookup"),
        schema=SCHEMA,
    )
    op.create_table(
        "token_counters",
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("last", sa.Integer, nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "pseudonymisations",
        sa.Column("id", sa.String(12), primary_key=True),
        sa.Column("email_id", sa.String(36), nullable=False, unique=True),
        sa.Column("thread_id", sa.String(36), nullable=False, index=True),
        sa.Column("counts", sa.JSON, nullable=False),
        sa.Column("signature_removed", sa.Boolean, nullable=False),
        sa.Column("quoted_removed", sa.Boolean, nullable=False),
        sa.Column("email", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "reveals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token", sa.String(40), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("thread_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
    for table in ("inbox", "outbox", "reveals", "pseudonymisations", "token_counters", "vault"):
        op.drop_table(table, schema=SCHEMA)
