"""V3: Kubernetes rollouts, dependency graph, anomaly consensus, SLO burn
rate, chaos drills.

This is the V3 schema delta applied on top of V1. ``deployments.status`` is
already VARCHAR(30) in V1, wide enough for CANARY_5..CANARY_100, so it needs
no change.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GUID = sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql")
JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    # --- new tables (chaos_experiments first: incidents references it) ---
    op.create_table(
        "chaos_experiments",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("fault_type", sa.String(50), nullable=False),
        sa.Column("injected_at", sa.DateTime),
        sa.Column("detection_latency_seconds", sa.Float),
        sa.Column("rollback_latency_seconds", sa.Float),
        sa.Column("passed", sa.Boolean),
        sa.Column("notes", sa.Text),
        sa.Column("magnitude", sa.Float),
        sa.Column("target", sa.String(10), nullable=False, server_default="all"),
        sa.Column("duration_s", sa.Integer),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING"),
        sa.Column("deployment_id", GUID, sa.ForeignKey("deployments.id")),
        sa.Column("recovery_latency_seconds", sa.Float),
        sa.Column("detection_bound_s", sa.Float),
        sa.Column("rollback_bound_s", sa.Float),
        sa.Column("affected_pods", JSONT),
        sa.Column("timeline", JSONT),
        sa.Column("completed_at", sa.DateTime),
    )
    op.create_table(
        "service_dependencies",
        sa.Column("id", GUID, primary_key=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("depends_on_service", sa.String(100), nullable=False),
        sa.UniqueConstraint("service_name", "depends_on_service"),
    )
    op.create_table(
        "anomaly_consensus",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("window_end", sa.DateTime, nullable=False),
        sa.Column("zscore_flagged", sa.Boolean),
        sa.Column("seasonal_flagged", sa.Boolean),
        sa.Column("isolation_forest_flagged", sa.Boolean),
        sa.Column("agreeing_layers", sa.Integer),
        sa.Column("auto_action_taken", sa.Boolean),
        sa.Column("details", JSONT),
    )
    op.create_index("idx_consensus_service_time", "anomaly_consensus", ["service_name", "window_end"])
    op.create_table(
        "slo_config",
        sa.Column("service_name", sa.String(100), primary_key=True),
        sa.Column("slo_target", sa.Float, nullable=False, server_default="0.999"),
        sa.Column("window_days", sa.Integer, nullable=False, server_default="30"),
        sa.Column("latency_threshold_ms", sa.Float, nullable=False, server_default="300"),
    )
    op.create_table(
        "sli_samples",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(10)),
        sa.Column("window_start", sa.DateTime, nullable=False),
        sa.Column("window_end", sa.DateTime, nullable=False),
        sa.Column("total_requests", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bad_requests", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_requests", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("idx_sli_service_time", "sli_samples", ["service_name", "window_end"])
    op.create_table(
        "burn_rate_alerts",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("long_window", sa.String(10), nullable=False),
        sa.Column("short_window", sa.String(10), nullable=False),
        sa.Column("burn_rate", sa.Float, nullable=False),
        sa.Column("short_burn_rate", sa.Float),
        sa.Column("fired_at", sa.DateTime),
        sa.Column("resolved_at", sa.DateTime),
    )

    # --- deltas on V1 tables ---
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("k8s_namespace", sa.String(100)))
        batch.add_column(sa.Column("k8s_deployment_name", sa.String(150)))
        batch.add_column(sa.Column("image", sa.String(200)))
        # Rows written by V1 were all instant blue-green switches.
        batch.add_column(sa.Column("strategy", sa.String(20), nullable=False, server_default="BLUE_GREEN"))
        batch.add_column(sa.Column("target_color", sa.String(10)))
        batch.add_column(sa.Column("stable_color", sa.String(10)))
        batch.add_column(sa.Column("auto_promote", sa.Boolean, nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("stage_started_at", sa.DateTime))
        batch.add_column(sa.Column("status_reason", sa.Text))
        batch.add_column(sa.Column("stage_history", JSONT))
        batch.add_column(sa.Column("finished_at", sa.DateTime))

    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("is_cascading", sa.Boolean, server_default=sa.false()))
        batch.add_column(sa.Column("root_incident_id", GUID))
        batch.add_column(sa.Column("blast_radius_services", JSONT))
        batch.add_column(sa.Column("detection_context", JSONT))
        batch.add_column(sa.Column("chaos_experiment_id", GUID))
        batch.create_foreign_key("fk_incidents_root_incident_id", "incidents", ["root_incident_id"], ["id"])
        batch.create_foreign_key(
            "fk_incidents_chaos_experiment_id", "chaos_experiments", ["chaos_experiment_id"], ["id"]
        )

    with op.batch_alter_table("metrics") as batch:
        batch.add_column(sa.Column("seasonal_residual_z", sa.Float))
        batch.add_column(sa.Column("isolation_forest_score", sa.Float))
    # The composite index serves service-only lookups too (leftmost prefix), so
    # V1's single-column index is dead weight on the hottest insert path.
    op.create_index("idx_metrics_service_time", "metrics", ["service_name", "recorded_at"])
    op.drop_index("ix_metrics_service_name", table_name="metrics")

    with op.batch_alter_table("logs") as batch:
        batch.add_column(sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"))
        batch.add_column(sa.Column("root_signature", sa.String(200)))

    with op.batch_alter_table("rca_reports") as batch:
        batch.add_column(sa.Column("signals", JSONT))


def downgrade() -> None:
    with op.batch_alter_table("rca_reports") as batch:
        batch.drop_column("signals")
    with op.batch_alter_table("logs") as batch:
        batch.drop_column("root_signature")
        batch.drop_column("occurrences")
    op.create_index("ix_metrics_service_name", "metrics", ["service_name"])
    op.drop_index("idx_metrics_service_time", table_name="metrics")
    with op.batch_alter_table("metrics") as batch:
        batch.drop_column("isolation_forest_score")
        batch.drop_column("seasonal_residual_z")
    with op.batch_alter_table("incidents") as batch:
        batch.drop_constraint("fk_incidents_chaos_experiment_id", type_="foreignkey")
        batch.drop_constraint("fk_incidents_root_incident_id", type_="foreignkey")
        for col in (
            "chaos_experiment_id",
            "detection_context",
            "blast_radius_services",
            "root_incident_id",
            "is_cascading",
        ):
            batch.drop_column(col)
    with op.batch_alter_table("deployments") as batch:
        for col in (
            "finished_at",
            "stage_history",
            "status_reason",
            "stage_started_at",
            "auto_promote",
            "stable_color",
            "target_color",
            "strategy",
            "image",
            "k8s_deployment_name",
            "k8s_namespace",
        ):
            batch.drop_column(col)
    op.drop_table("burn_rate_alerts")
    op.drop_index("idx_sli_service_time", table_name="sli_samples")
    op.drop_table("sli_samples")
    op.drop_table("slo_config")
    op.drop_index("idx_consensus_service_time", table_name="anomaly_consensus")
    op.drop_table("anomaly_consensus")
    op.drop_table("service_dependencies")
    op.drop_table("chaos_experiments")
