"""data requests, erased emails, and when a vault entry was last used

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from privacy_service.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column(
        "vault", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA
    )
    op.execute(f"update {SCHEMA}.vault set last_used_at = created_at")
    op.alter_column("vault", "last_used_at", nullable=False, schema=SCHEMA)
    op.create_table(
        "data_requests",
        sa.Column("id", sa.String(12), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, index=True),
        sa.Column("received_on", sa.Date, nullable=False),
        sa.Column("due_on", sa.Date, nullable=False),
        sa.Column("address_enc", sa.Text, nullable=True),
        sa.Column("subject_token", sa.String(40), nullable=True),
        sa.Column("verified_how", sa.Text, nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_stores", sa.JSON, nullable=False),
        sa.Column("erased", sa.JSON, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_table(
        "erased_emails",
        sa.Column("email_id", sa.String(36), primary_key=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("erased_emails", schema=SCHEMA)
    op.drop_table("data_requests", schema=SCHEMA)
    op.drop_column("vault", "last_used_at", schema=SCHEMA)
