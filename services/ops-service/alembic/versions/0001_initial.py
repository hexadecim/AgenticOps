"""fix proposals and jobs, probe states, runbook index (pgvector), mock environment state, outbox, inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from ops_service.config import Settings
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "fix_proposals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("choices", sa.JSON, nullable=False),
        sa.Column("services", sa.JSON, nullable=False),
        sa.Column("region", sa.String(40)),
        sa.Column("site", sa.String(40)),
        sa.Column("jira_key", sa.String(40)),
        schema=SCHEMA,
    )
    op.create_table(
        "fix_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("jira_key", sa.String(40)),
        sa.Column("region", sa.String(40)),
        sa.Column("ok", sa.Boolean),
        sa.Column("detail", sa.Text),
        schema=SCHEMA,
    )
    op.create_table(
        "probe_states",
        sa.Column("probe", sa.String(60), primary_key=True),
        sa.Column("region", sa.String(40), primary_key=True),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "runbook_index",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "runbook_chunks",
        sa.Column("runbook_id", sa.String(40), primary_key=True),
        sa.Column("idx", sa.Integer, primary_key=True),
        sa.Column("heading", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(), nullable=False),  # pgvector (extension created in init.sql)
        schema=SCHEMA,
    )
    op.create_table(
        "mock_faults",
        sa.Column("service", sa.String(60), primary_key=True),
        sa.Column("region", sa.String(40), primary_key=True),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    op.create_table(
        "mock_runs",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
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
    for table in (
        "inbox", "outbox", "mock_runs", "mock_faults", "runbook_chunks", "runbook_index", "probe_states",
        "fix_jobs", "fix_proposals",
    ):  # fmt: skip
        op.drop_table(table, schema=SCHEMA)
