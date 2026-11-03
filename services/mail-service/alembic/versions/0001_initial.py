"""emails, poll_state, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from mail_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "emails",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedupe_key", sa.String(600), nullable=False, unique=True),
        sa.Column("thread_id", sa.String(36), nullable=False, index=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_id", sa.String(255), nullable=False),
        sa.Column("provider_thread_id", sa.String(255), index=True),
        sa.Column("message_id", sa.String(512), index=True),
        sa.Column("in_reply_to", sa.String(512)),
        sa.Column("references", sa.JSON, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("sender", sa.JSON, nullable=False),
        sa.Column("recipients", sa.JSON, nullable=False),
        sa.Column("body_text", sa.Text, nullable=False),
        sa.Column("body_html", sa.Text),
        sa.Column("attachments", sa.JSON, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, index=True),
        schema=SCHEMA,
    )
    op.create_table(
        "poll_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("cursor", sa.JSON),
        sa.Column("last_poll_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, nullable=False),
        sa.Column("last_new_count", sa.Integer, nullable=False),
        sa.Column("total_received", sa.Integer, nullable=False),
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
    for table in ("inbox", "outbox", "poll_state", "emails"):
        op.drop_table(table, schema=SCHEMA)
