"""V1 baseline: the six tables V1 created with ``Base.metadata.create_all``.

A database created by V1 already has exactly this shape; ``init_db`` stamps
such a database at this revision instead of re-running it, then upgrades.

Revision ID: 0001
Revises:
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Column types are declared here rather than imported from app.models so this
# revision keeps describing the V1 schema however the models evolve.
GUID = sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")
JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("config_diff", JSONT),
        sa.Column("previous_version_id", GUID, sa.ForeignKey("deployments.id")),
        sa.Column("deployed_at", sa.DateTime),
        sa.Column("switched_at", sa.DateTime),
    )
    op.create_table(
        "incidents",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("detected_at", sa.DateTime),
        sa.Column("resolved_at", sa.DateTime),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("severity", sa.String(20)),
        sa.Column("trigger_reason", sa.Text, nullable=False),
        sa.Column("related_deployment_id", GUID, sa.ForeignKey("deployments.id")),
    )
    op.create_table(
        "metrics",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("metric_name", sa.String(50), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("z_score", sa.Float),
        sa.Column("recorded_at", sa.DateTime),
    )
    op.create_index("ix_metrics_service_name", "metrics", ["service_name"])
    op.create_index("ix_metrics_recorded_at", "metrics", ["recorded_at"])
    op.create_table(
        "logs",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("error_signature", sa.String(200)),
        sa.Column("incident_id", GUID, sa.ForeignKey("incidents.id")),
        sa.Column("logged_at", sa.DateTime),
    )
    op.create_table(
        "rca_reports",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("incident_id", GUID, sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("root_cause_candidates", JSONT, nullable=False),
        sa.Column("llm_model_used", sa.String(50)),
        sa.Column("generated_at", sa.DateTime),
    )
    op.create_table(
        "business_impact",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("incident_id", GUID, sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("affected_requests", sa.Integer),
        sa.Column("error_rate_delta", sa.Float),
        sa.Column("estimated_impact_value", sa.Float),
        sa.Column("calculation_basis", JSONT),
        sa.Column("calculated_at", sa.DateTime),
    )


def downgrade() -> None:
    for table in ("business_impact", "rca_reports", "logs", "metrics", "incidents", "deployments"):
        op.drop_table(table)
