"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# A version becomes a Kubernetes label value, so it must match the label grammar.
VERSION_PATTERN = r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"


# ---------- Deployments ----------
class DeploymentCreate(BaseModel):
    service_name: str
    version: str = Field(..., pattern=VERSION_PATTERN)
    strategy: Literal["CANARY", "BLUE_GREEN"] = "CANARY"
    config_diff: Optional[dict[str, Any]] = None
    image: Optional[str] = Field(None, description="Container image; defaults to the demo workload image")
    auto_promote: bool = True
    previous_version_id: Optional[str] = None
    # Ships the candidate with a bad config (a share of its requests fail).
    # Detection never sees this flag — only the resulting traffic.
    simulate_failure: bool = False


class DeploymentOut(ORMModel):
    id: str
    service_name: str
    version: str
    status: str
    strategy: Optional[str] = None
    config_diff: Optional[dict[str, Any]] = None
    previous_version_id: Optional[str] = None
    deployed_at: Optional[datetime] = None
    switched_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    k8s_namespace: Optional[str] = None
    k8s_deployment_name: Optional[str] = None
    image: Optional[str] = None
    target_color: Optional[str] = None
    stable_color: Optional[str] = None
    auto_promote: Optional[bool] = None
    stage_started_at: Optional[datetime] = None
    status_reason: Optional[str] = None
    stage_history: Optional[list[dict[str, Any]]] = None
    canary_weight: int = 0


class PromoteResult(BaseModel):
    action: str
    verdict: dict[str, Any]
    deployment: DeploymentOut


class K8sRolloutStatus(BaseModel):
    deployment_id: str
    cluster_mode: str
    namespace: str
    live_color: Optional[str]
    routing: dict[str, Any]
    candidate: Optional[dict[str, Any]]
    stable: Optional[dict[str, Any]]
    gate: Optional[dict[str, Any]] = None


# ---------- Metrics ----------
class MetricIngest(BaseModel):
    service_name: str
    metric_name: str = Field(..., description="error_rate | latency_p95 | cpu_usage | memory_usage")
    value: float


class MetricBatchIngest(BaseModel):
    """One reading of all four metrics — what the anomaly suite evaluates."""

    service_name: str
    error_rate: float
    latency_p95: float
    cpu_usage: float
    memory_usage: float
    requests: int = Field(100, ge=1, description="requests behind this reading (for the error-ratio feature)")


class MetricOut(ORMModel):
    id: int
    service_name: str
    metric_name: str
    value: float
    z_score: Optional[float] = None
    seasonal_residual_z: Optional[float] = None
    isolation_forest_score: Optional[float] = None
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
    is_cascading: Optional[bool] = False
    root_incident_id: Optional[str] = None
    blast_radius_services: Optional[list[str]] = None
    chaos_experiment_id: Optional[str] = None


class LogOut(ORMModel):
    id: int
    service_name: str
    level: str
    message: str
    error_signature: Optional[str] = None
    root_signature: Optional[str] = None
    occurrences: int = 1
    logged_at: Optional[datetime] = None


class IncidentDetail(IncidentOut):
    detection_context: Optional[dict[str, Any]] = None
    timeline: list[dict[str, Any]] = []
    logs: list[LogOut] = []
    metrics: list[MetricOut] = []


class CascadeTree(BaseModel):
    root: IncidentOut
    children: list[IncidentOut]
    edges: list[dict[str, str]]  # parent -> child service edges along the dependency graph


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
    signals: Optional[dict[str, Any]] = None


# ---------- Business impact ----------
class BusinessImpactOut(ORMModel):
    id: str
    incident_id: str
    affected_requests: Optional[int] = None
    error_rate_delta: Optional[float] = None
    estimated_impact_value: Optional[float] = None
    calculation_basis: Optional[dict[str, Any]] = None
    calculated_at: Optional[datetime] = None


# ---------- Services / SLO ----------
class SLOUpdate(BaseModel):
    slo_target: Optional[float] = Field(None, gt=0.5, lt=1.0)
    window_days: Optional[int] = Field(None, ge=1, le=90)
    latency_threshold_ms: Optional[float] = Field(None, gt=0)


class EdgeCreate(BaseModel):
    service_name: str
    depends_on_service: str


# ---------- Chaos ----------
class ChaosInject(BaseModel):
    service_name: str
    fault_type: Literal["LATENCY_INJECTION", "ERROR_BURST", "POD_KILL"]
    magnitude: Optional[float] = Field(
        None, description="ms of added latency | share of requests failing | share of pods killed"
    )
    target: Literal["all", "stable", "canary"] = "all"
    duration_s: Optional[int] = Field(None, ge=10, le=900)
    detection_bound_s: Optional[float] = Field(None, gt=0)
    rollback_bound_s: Optional[float] = Field(None, gt=0)


class ChaosExperimentOut(ORMModel):
    id: str
    service_name: str
    fault_type: str
    magnitude: Optional[float] = None
    target: str
    status: str
    injected_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_s: Optional[int] = None
    detection_latency_seconds: Optional[float] = None
    rollback_latency_seconds: Optional[float] = None
    recovery_latency_seconds: Optional[float] = None
    detection_bound_s: Optional[float] = None
    rollback_bound_s: Optional[float] = None
    passed: Optional[bool] = None
    notes: Optional[str] = None
    deployment_id: Optional[str] = None
    affected_pods: Optional[list[dict[str, Any]]] = None
    timeline: Optional[list[dict[str, Any]]] = None


# ---------- n8n callback ----------
class N8NCallback(BaseModel):
    incident_id: str
    rca_report: Optional[dict[str, Any]] = None
    business_impact: Optional[dict[str, Any]] = None
    notified: bool = False
