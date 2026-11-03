"""approvals, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from approval_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, index=True),
        sa.Column("thread_id", sa.String(36), nullable=False, index=True),
        sa.Column("gate", sa.String(10), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, index=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("site", sa.String(80)),
        sa.Column("severity", sa.String(8)),
        sa.Column("jira_key", sa.String(40)),
        sa.Column("proposal", sa.JSON, nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(36)),
        sa.Column("decided_by_role", sa.String(40)),
        sa.Column("reason", sa.Text),
        sa.Column("edits", sa.JSON, nullable=False),
        sa.Column("outcome", sa.String(20)),
        sa.Column("outcome_detail", sa.Text),
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
    for table in ("inbox", "outbox", "approvals"):
        op.drop_table(table, schema=SCHEMA)
