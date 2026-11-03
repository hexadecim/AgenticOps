"""proposals, jobs, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from jira_connector.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("ticket", sa.JSON, nullable=False),
        sa.Column("link_to", sa.String(40)),
        sa.Column("reporter_token", sa.String(40)),
        schema=SCHEMA,
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("ticket", sa.JSON, nullable=False),
        sa.Column("link_to", sa.String(40)),
        sa.Column("reporter_token", sa.String(40)),
        sa.Column("status", sa.String(10), nullable=False, index=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("issue_key", sa.String(40)),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, index=True),
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
    for table in ("inbox", "outbox", "jobs", "proposals"):
        op.drop_table(table, schema=SCHEMA)
