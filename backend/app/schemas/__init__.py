"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Deployments ----------
class DeploymentCreate(BaseModel):
    service_name: str
    version: str
    config_diff: Optional[dict[str, Any]] = None
    previous_version_id: Optional[str] = None
    # Simulation controls: force a bad deploy to demo the rollback path.
    simulate_failure: bool = False


class DeploymentOut(ORMModel):
    id: str
    service_name: str
    version: str
    status: str
    config_diff: Optional[dict[str, Any]] = None
    previous_version_id: Optional[str] = None
    deployed_at: Optional[datetime] = None
    switched_at: Optional[datetime] = None


# ---------- Metrics ----------
class MetricIngest(BaseModel):
    service_name: str
    metric_name: str = Field(..., description="error_rate | latency_p95 | cpu_usage | memory_usage")
    value: float


class MetricOut(ORMModel):
    id: int
    service_name: str
    metric_name: str
    value: float
    z_score: Optional[float] = None
    recorded_at: Optional[datetime] = None


# ---------- Incidents ----------
class IncidentOut(ORMModel):
    id: str
    service_name: str
    detected_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    status: str
    severity: Optional[str] = None
    trigger_reason: str
    related_deployment_id: Optional[str] = None


class LogOut(ORMModel):
    id: int
    service_name: str
    level: str
    message: str
    error_signature: Optional[str] = None
    logged_at: Optional[datetime] = None


class IncidentDetail(IncidentOut):
    timeline: list[dict[str, Any]] = []
    logs: list[LogOut] = []
    metrics: list[MetricOut] = []


# ---------- RCA ----------
class RootCauseCandidate(BaseModel):
    cause: str
    confidence: str  # low | medium | high
    evidence: str


class RCAReportOut(ORMModel):
    id: str
    incident_id: str
    root_cause_candidates: list[RootCauseCandidate]
    llm_model_used: Optional[str] = None
    generated_at: Optional[datetime] = None


# ---------- Business impact ----------
class BusinessImpactOut(ORMModel):
    id: str
    incident_id: str
    affected_requests: Optional[int] = None
    error_rate_delta: Optional[float] = None
    estimated_impact_value: Optional[float] = None
    calculation_basis: Optional[dict[str, Any]] = None
    calculated_at: Optional[datetime] = None


# ---------- n8n callback ----------
class N8NCallback(BaseModel):
    incident_id: str
    rca_report: Optional[dict[str, Any]] = None
    business_impact: Optional[dict[str, Any]] = None
    notified: bool = False
