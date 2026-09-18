"""SQLAlchemy ORM models for OpsForge Nexus.

Portable types: GUID + JSONType resolve to native ``UUID``/``JSONB`` on
PostgreSQL and to ``CHAR(36)``/``JSON`` on SQLite (used by the test suite),
so the same models power both prod and tests without schema drift.

Schema changes ship as Alembic migrations (``backend/migrations``); the V3
delta on top of the V1 tables is revision ``0002``.
"""

import uuid

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.clock import utcnow
from app.db.session import Base


class GUID(TypeDecorator):
    """Platform-independent UUID: native on Postgres, CHAR(36) elsewhere."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=False))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        return value


# JSONB on Postgres, generic JSON (text-backed) on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")
# BIGSERIAL on Postgres; SQLite only autoincrements INTEGER primary keys.
BigID = BigInteger().with_variant(Integer, "sqlite")


def _uuid() -> str:
    return str(uuid.uuid4())


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    version = Column(String(50), nullable=False)
    # IDLE, PREPARING_GREEN, DEPLOYING_GREEN, HEALTH_CHECKING,
    # CANARY_5, CANARY_25, CANARY_50, CANARY_100,
    # TRAFFIC_SWITCHING, LIVE, ROLLING_BACK, ROLLED_BACK
    status = Column(String(30), nullable=False, default="IDLE")
    config_diff = Column(JSONType)
    previous_version_id = Column(GUID(), ForeignKey("deployments.id"))
    deployed_at = Column(DateTime, default=utcnow)
    switched_at = Column(DateTime)

    # --- V3: Kubernetes-backed rollout ---
    k8s_namespace = Column(String(100))
    k8s_deployment_name = Column(String(150))
    image = Column(String(200))  # container image rolled out (null: the demo default)
    strategy = Column(String(20), nullable=False, default="CANARY")  # CANARY | BLUE_GREEN
    target_color = Column(String(10))  # colour the new version is rolled out to
    stable_color = Column(String(10))  # colour serving traffic when the rollout began
    auto_promote = Column(Boolean, nullable=False, default=True)
    stage_started_at = Column(DateTime)  # start of the current stage's confirmation window
    status_reason = Column(Text)  # why it is in its current state (gate verdict, rollout failure)
    stage_history = Column(JSONType)  # [{stage, at, gate}] audit trail of every transition
    finished_at = Column(DateTime)


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    detected_at = Column(DateTime, default=utcnow)
    resolved_at = Column(DateTime)
    status = Column(String(30), nullable=False, default="OPEN")  # OPEN, INVESTIGATING, RESOLVED
    severity = Column(String(20))  # LOW, MEDIUM, HIGH, CRITICAL
    trigger_reason = Column(Text, nullable=False)
    related_deployment_id = Column(GUID(), ForeignKey("deployments.id"))

    # --- V3: dependency-aware cascade detection ---
    is_cascading = Column(Boolean, default=False)
    root_incident_id = Column(GUID(), ForeignKey("incidents.id"))
    blast_radius_services = Column(JSONType)  # downstream services reachable from this one
    # Detector-layer agreement + burn rate at the moment the incident opened.
    detection_context = Column(JSONType)
    chaos_experiment_id = Column(GUID(), ForeignKey("chaos_experiments.id"))


class Metric(Base):
    __tablename__ = "metrics"
    __table_args__ = (Index("idx_metrics_service_time", "service_name", "recorded_at"),)

    id = Column(BigID, primary_key=True, autoincrement=True)
    # (service_name, recorded_at) is covered by idx_metrics_service_time.
    service_name = Column(String(100), nullable=False)
    metric_name = Column(String(50), nullable=False)  # error_rate, latency_p95, cpu_usage, memory_usage
    value = Column(Float, nullable=False)
    z_score = Column(Float)
    recorded_at = Column(DateTime, default=utcnow, index=True)
    # --- V3: anomaly-suite layer outputs for this reading ---
    seasonal_residual_z = Column(Float)  # z-score of the STL residual
    isolation_forest_score = Column(Float)  # lower = more anomalous


class Log(Base):
    __tablename__ = "logs"

    id = Column(BigID, primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    level = Column(String(10), nullable=False)  # INFO, WARN, ERROR
    message = Column(Text, nullable=False)
    error_signature = Column(String(200))  # dedup key
    incident_id = Column(GUID(), ForeignKey("incidents.id"))
    logged_at = Column(DateTime, default=utcnow)
    # V3: the gateway writes one row per (signature, tick) rather than one per
    # failed request; counts sum this column.
    occurrences = Column(Integer, nullable=False, default=1)
    # For errors propagated from a dependency: the signature the service that
    # failed first reported (e.g. checkout's 502 caused by ledger's 500).
    root_signature = Column(String(200))


class RCAReport(Base):
    __tablename__ = "rca_reports"

    id = Column(GUID(), primary_key=True, default=_uuid)
    incident_id = Column(GUID(), ForeignKey("incidents.id"), nullable=False)
    root_cause_candidates = Column(JSONType, nullable=False)  # [{cause, confidence, evidence}]
    llm_model_used = Column(String(50))
    generated_at = Column(DateTime, default=utcnow)
    # V3: the exact structured context the ranking was computed over.
    signals = Column(JSONType)


class BusinessImpact(Base):
    __tablename__ = "business_impact"

    id = Column(GUID(), primary_key=True, default=_uuid)
    incident_id = Column(GUID(), ForeignKey("incidents.id"), nullable=False)
    affected_requests = Column(Integer)
    error_rate_delta = Column(Float)
    estimated_impact_value = Column(Float)
    calculation_basis = Column(JSONType)  # audit trail of assumptions/inputs
    calculated_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------- V3 tables --


class ServiceDependency(Base):
    """Edge of the hand-defined dependency DAG: ``service_name`` depends on
    ``depends_on_service``."""

    __tablename__ = "service_dependencies"
    __table_args__ = (UniqueConstraint("service_name", "depends_on_service"),)

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    depends_on_service = Column(String(100), nullable=False)


class AnomalyConsensus(Base):
    __tablename__ = "anomaly_consensus"
    __table_args__ = (Index("idx_consensus_service_time", "service_name", "window_end"),)

    id = Column(BigID, primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    window_end = Column(DateTime, nullable=False)
    zscore_flagged = Column(Boolean)
    seasonal_flagged = Column(Boolean)
    isolation_forest_flagged = Column(Boolean)
    agreeing_layers = Column(Integer)  # 0-3
    auto_action_taken = Column(Boolean)  # true only if agreeing_layers >= 2
    details = Column(JSONType)  # per-layer evidence (metrics, scores, warm-up state)


class SLOConfig(Base):
    __tablename__ = "slo_config"

    service_name = Column(String(100), primary_key=True)
    slo_target = Column(Float, nullable=False, default=0.999)
    window_days = Column(Integer, nullable=False, default=30)
    # A request is "good" if it is not a 5xx AND it completes under this bound.
    latency_threshold_ms = Column(Float, nullable=False, default=300.0)


class SLISample(Base):
    """Request counts per service, colour and control-loop tick — the raw
    material every burn-rate window is summed from."""

    __tablename__ = "sli_samples"
    __table_args__ = (Index("idx_sli_service_time", "service_name", "window_end"),)

    id = Column(BigID, primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    color = Column(String(10))  # which colour served the requests (null = unknown)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    total_requests = Column(Integer, nullable=False, default=0)
    bad_requests = Column(Integer, nullable=False, default=0)  # 5xx OR slower than threshold
    error_requests = Column(Integer, nullable=False, default=0)  # 5xx / transport errors only


class BurnRateAlert(Base):
    __tablename__ = "burn_rate_alerts"

    id = Column(BigID, primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    severity = Column(String(20), nullable=False)  # FAST_BURN, MEDIUM_BURN, SLOW_BURN
    long_window = Column(String(10), nullable=False)  # '1h', '6h', '3d'
    short_window = Column(String(10), nullable=False)  # '5m', '30m', '6h'
    burn_rate = Column(Float, nullable=False)  # long-window burn rate when it fired
    short_burn_rate = Column(Float)
    fired_at = Column(DateTime, default=utcnow)
    resolved_at = Column(DateTime)


class ChaosExperiment(Base):
    __tablename__ = "chaos_experiments"

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    fault_type = Column(String(50), nullable=False)  # LATENCY_INJECTION, ERROR_BURST, POD_KILL
    injected_at = Column(DateTime, default=utcnow)
    detection_latency_seconds = Column(Float)  # injection -> first consensus flag
    rollback_latency_seconds = Column(Float)  # injection -> rollback complete (if applicable)
    passed = Column(Boolean)
    notes = Column(Text)
    # --- beyond the base spec: what was injected where, and how it ended ---
    magnitude = Column(Float)
    target = Column(String(10), nullable=False, default="all")  # all | stable | canary
    duration_s = Column(Integer)
    status = Column(String(20), nullable=False, default="RUNNING")  # RUNNING, COMPLETED
    deployment_id = Column(GUID(), ForeignKey("deployments.id"))  # set when deployment-linked
    recovery_latency_seconds = Column(Float)  # injection -> service healthy again
    detection_bound_s = Column(Float)
    rollback_bound_s = Column(Float)
    affected_pods = Column(JSONType)
    timeline = Column(JSONType)  # [{at, event}] measured milestones
    completed_at = Column(DateTime)


__all__ = [
    "Deployment",
    "Incident",
    "Metric",
    "Log",
    "RCAReport",
    "BusinessImpact",
    "ServiceDependency",
    "AnomalyConsensus",
    "SLOConfig",
    "SLISample",
    "BurnRateAlert",
    "ChaosExperiment",
]
