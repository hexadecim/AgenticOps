"""approved customer messages waiting to go out (tokenised), and the approvals seen for them

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from mail_service.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "outgoing",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False, index=True),
        sa.Column("approval_id", sa.String(36), nullable=False, index=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("email_id", sa.String(36), nullable=False),
        sa.Column("email_token", sa.String(40), nullable=False),
        sa.Column("name_token", sa.String(40)),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("jira_key", sa.String(40)),
        sa.Column("kind", sa.String(20)),
        sa.Column("status", sa.String(10), nullable=False, index=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("next_try_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", "email_token", name="uq_outgoing_message_recipient"),
        schema=SCHEMA,
    )
    op.create_table(
        "approved_messages",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False, index=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("approved_messages", schema=SCHEMA)
    op.drop_table("outgoing", schema=SCHEMA)
