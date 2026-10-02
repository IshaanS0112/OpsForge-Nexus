"""The rollback cooldown moves out of process memory into the database.

V1 kept the anti-flapping window in a dictionary on the engine object: a
restart forgot it, and a second worker never saw it. One row per service,
overwritten by each rollback.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

GUID = sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")


def upgrade() -> None:
    op.create_table(
        "rollback_cooldowns",
        sa.Column("service_name", sa.String(100), primary_key=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("until_at", sa.DateTime, nullable=False),
        sa.Column("trigger", sa.String(40)),
        sa.Column("deployment_id", GUID, sa.ForeignKey("deployments.id")),
    )


def downgrade() -> None:
    op.drop_table("rollback_cooldowns")
