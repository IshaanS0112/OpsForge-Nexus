"""SQLAlchemy ORM models for OpsForge Nexus.

Portable types: GUID + JSONType resolve to native ``UUID``/``JSONB`` on
PostgreSQL and to ``CHAR(36)``/``JSON`` on SQLite (used by the test suite),
so the same models power both prod and tests without schema drift.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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


def _uuid() -> str:
    return str(uuid.uuid4())


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    version = Column(String(50), nullable=False)
    # IDLE, PREPARING_GREEN, DEPLOYING_GREEN, HEALTH_CHECKING,
    # TRAFFIC_SWITCHING, LIVE, ROLLING_BACK, ROLLED_BACK
    status = Column(String(30), nullable=False, default="IDLE")
    config_diff = Column(JSONType)
    previous_version_id = Column(GUID(), ForeignKey("deployments.id"))
    deployed_at = Column(DateTime, default=datetime.utcnow)
    switched_at = Column(DateTime)


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(GUID(), primary_key=True, default=_uuid)
    service_name = Column(String(100), nullable=False)
    detected_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime)
    status = Column(String(30), nullable=False, default="OPEN")  # OPEN, INVESTIGATING, RESOLVED
    severity = Column(String(20))  # LOW, MEDIUM, HIGH, CRITICAL
    trigger_reason = Column(Text, nullable=False)
    related_deployment_id = Column(GUID(), ForeignKey("deployments.id"))


class Metric(Base):
    __tablename__ = "metrics"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False, index=True)
    metric_name = Column(String(50), nullable=False)  # error_rate, latency_p95, cpu_usage, memory_usage
    value = Column(Float, nullable=False)
    z_score = Column(Float)
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)


class Log(Base):
    __tablename__ = "logs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    level = Column(String(10), nullable=False)  # INFO, WARN, ERROR
    message = Column(Text, nullable=False)
    error_signature = Column(String(200))  # dedup key
    incident_id = Column(GUID(), ForeignKey("incidents.id"))
    logged_at = Column(DateTime, default=datetime.utcnow)


class RCAReport(Base):
    __tablename__ = "rca_reports"

    id = Column(GUID(), primary_key=True, default=_uuid)
    incident_id = Column(GUID(), ForeignKey("incidents.id"), nullable=False)
    root_cause_candidates = Column(JSONType, nullable=False)  # [{cause, confidence, evidence}]
    llm_model_used = Column(String(50))
    generated_at = Column(DateTime, default=datetime.utcnow)


class BusinessImpact(Base):
    __tablename__ = "business_impact"

    id = Column(GUID(), primary_key=True, default=_uuid)
    incident_id = Column(GUID(), ForeignKey("incidents.id"), nullable=False)
    affected_requests = Column(Integer)
    error_rate_delta = Column(Float)
    estimated_impact_value = Column(Float)
    calculation_basis = Column(JSONType)  # audit trail of assumptions/inputs
    calculated_at = Column(DateTime, default=datetime.utcnow)


__all__ = [
    "Deployment",
    "Incident",
    "Metric",
    "Log",
    "RCAReport",
    "BusinessImpact",
]
