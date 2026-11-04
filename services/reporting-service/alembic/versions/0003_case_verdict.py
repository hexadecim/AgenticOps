"""a case's health verdict, kept apart from its status (the run goes on to the customer message)

Revision ID: 0003
"""

import sqlalchemy as sa
from alembic import op
from reporting_service.config import Settings

revision = "0003"
down_revision = "0002"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.add_column("cases", sa.Column("verdict", sa.String(20)), schema=SCHEMA)
    op.add_column("cases", sa.Column("verdict_at", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.execute(
        f"update {SCHEMA}.cases set verdict = status, verdict_at = updated_at "
        "where status in ('resolved', 'fix_failed')"
    )


def downgrade() -> None:
    op.drop_column("cases", "verdict_at", schema=SCHEMA)
    op.drop_column("cases", "verdict", schema=SCHEMA)
