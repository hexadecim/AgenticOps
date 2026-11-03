"""cases, emails, timeline, dependencies, inbox (read models)

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from reporting_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]
TIMES = ("received_at", "queued_at", "triaged_at", "requested_at", "decided_at", "ticket_at", "updated_at")


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), nullable=False, index=True),
        sa.Column("email_id", sa.String(36), index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        *(sa.Column(name, sa.DateTime(timezone=True)) for name in TIMES),
        sa.Column("decision", sa.String(12)),
        sa.Column("outcome", sa.String(12)),
        sa.Column("jira_key", sa.String(40), index=True),
        sa.Column("title", sa.Text),
        sa.Column("severity", sa.String(8)),
        sa.Column("site", sa.String(80)),
        sa.Column("paused", sa.Boolean, nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "emails",
        sa.Column("email_id", sa.String(36), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "timeline",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("run_id", sa.String(36), index=True),
        sa.Column("thread_id", sa.String(36), index=True),
        sa.Column("jira_key", sa.String(40)),
        sa.Column("system", sa.Boolean, nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "dependencies",
        sa.Column("name", sa.String(60), primary_key=True),
        sa.Column("states", sa.JSON, nullable=False),
        sa.Column("since", sa.DateTime(timezone=True)),
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
    for table in ("inbox", "dependencies", "timeline", "emails", "cases"):
        op.drop_table(table, schema=SCHEMA)
