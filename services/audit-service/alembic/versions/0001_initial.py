"""entries (append-only, enforced by triggers), inbox

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from audit_service.config import Settings

revision = "0001"
down_revision = None
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, index=True),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("actor_id", sa.String(80)),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("privacy", sa.String(200), nullable=False),
        sa.Column("ticket", sa.String(40), index=True),
        sa.Column("thread_id", sa.String(36), index=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("payload", sa.JSON, nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_entries_page", "entries", [sa.text("occurred_at DESC"), sa.text("id DESC")], schema=SCHEMA
    )
    op.create_table(
        "inbox",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("consumer", sa.String(80), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    # Append-only: any UPDATE, DELETE or TRUNCATE on the log fails, whoever issues it (CLAUDE.md rule 8).
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.audit_entries_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit log is append-only: % is not allowed on %', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
        END $$;
        """
    )
    op.execute(
        f"CREATE TRIGGER entries_no_update_delete BEFORE UPDATE OR DELETE ON {SCHEMA}.entries "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.audit_entries_append_only()"
    )
    op.execute(
        f"CREATE TRIGGER entries_no_truncate BEFORE TRUNCATE ON {SCHEMA}.entries "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.audit_entries_append_only()"
    )


def downgrade() -> None:
    raise RuntimeError("The audit log is never dropped by a migration.")
