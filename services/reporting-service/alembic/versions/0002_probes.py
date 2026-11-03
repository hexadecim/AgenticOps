"""probes (health probes of the supported systems, for Platform health)

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op
from reporting_service.config import Settings

revision = "0002"
down_revision = "0001"
SCHEMA = Settings().db_schema  # type: ignore[call-arg]


def upgrade() -> None:
    op.create_table(
        "probes",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("region", sa.String(40), primary_key=True),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("since", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("probes", schema=SCHEMA)
