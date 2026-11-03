"""runs, outbox, inbox (LangGraph creates its checkpoint tables itself)

Revision ID: 0001
"""

import sqlalchemy as sa
from agent_orchestrator.config import Settings
from alembic import op

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email_id", sa.String(36), nullable=False, unique=True),
        sa.Column("thread_id", sa.String(36), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("node", sa.String(40)),
        sa.Column("detail", sa.Text),
        sa.Column("approval_id", sa.String(36), index=True),
        sa.Column("jira_key", sa.String(40)),
        sa.Column("decision", sa.JSON),
        sa.Column("attempts", sa.Integer, nullable=False),
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
    for table in ("inbox", "outbox", "runs"):
        op.drop_table(table, schema=SCHEMA)
