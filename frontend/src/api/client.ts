// Typed client for the OpsForge backend. Everything goes through /api (Vite's
// dev proxy, or nginx in Docker).

const BASE = import.meta.env.VITE_API_BASE || "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? body);
  return body as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

// ---------------------------------------------------------------- types --
export type Status = "ok" | "warn" | "critical" | "rollback" | "canary";
export type Color = "blue" | "green";
export type MetricName = "error_rate" | "latency_p95" | "cpu_usage" | "memory_usage";

export interface LayerDetail {
  layer: "zscore" | "seasonal" | "isolation_forest";
  flagged: boolean;
  warm: boolean;
  z?: Partial<Record<MetricName, number>>;
  residual_z?: Partial<Record<MetricName, number>>;
  expected?: Partial<Record<MetricName, number>>;
  streak?: Record<string, number> | number;
  warmup?: Partial<Record<MetricName, string>>;
  training?: string;
  score?: number;
  threshold?: number;
  trained_on?: number;
  period_s?: number;
}

export interface Consensus {
  zscore_flagged: boolean;
  seasonal_flagged: boolean;
  isolation_forest_flagged: boolean;
  agreeing_layers: number;
  auto_action: boolean;
  layers: Record<"zscore" | "seasonal" | "isolation_forest", LayerDetail>;
}

export interface GateVerdict {
  verdict: "PASS" | "FAIL" | "WAIT";
  reason: string;
  canary_requests: number;
  canary_bad: number;
  canary_burn: number | null;
  sprt_llr: number;
  sprt_fail_at: number;
  stable_requests: number;
  stable_bad: number;
  stable_burn: number | null;
  threshold: number;
  min_requests: number;
  elapsed_s: number;
  window_s: number;
}

export interface ServiceState {
  name: string;
  tier: string | null;
  layer: number;
  status: Status;
  upstream: string[];
  downstream: string[];
  live_version: string;
  route: { live_color: Color; canary_color: Color | null; canary_weight: number };
  values: Record<MetricName, number | null> | null;
  traffic: { requests: number; bad: number; errors: number; rps: number } | null;
  consensus: Consensus | null;
  burn: {
    worst: BurnSeverity | null;
    fast_long: number | null;
    fast_short: number | null;
    budget_remaining: number | null;
    slo_target: number;
    latency_threshold_ms: number;
  } | null;
  incident: {
    id: string;
    status: string;
    severity: string | null;
    is_cascading: boolean;
    root_incident_id: string | null;
  } | null;
  deployment: {
    id: string;
    version: string;
    status: string;
    strategy: string;
    weight: number;
    target_color: Color;
    status_reason: string | null;
    gate: GateVerdict | null;
  } | null;
}

export type BurnSeverity = "FAST_BURN" | "MEDIUM_BURN" | "SLOW_BURN";

export interface Snapshot {
  generated_at: string;
  cluster: { mode: "kind" | "simulated"; namespace: string; bootstrapped: boolean; error: string | null };
  loop: { ticks: number; last_tick_at: string | null; last_tick_ms: number | null; tick_s: number };
  summary: {
    services: number;
    healthy: number;
    open_incidents: number;
    worst_burn: { service: string; severity: BurnSeverity; burn: number } | null;
    max_burn_1h: { service: string; burn: number } | null;
    active_rollouts: number;
    running_drills: number;
  };
  services: ServiceState[];
  edges: { service: string; depends_on: string }[];
  drills: {
    id: string;
    service_name: string;
    fault_type: FaultType;
    target: string;
    injected_at: string;
    detection_latency_seconds: number | null;
    rollback_latency_seconds: number | null;
  }[];
}

export interface ClusterInfo {
  mode: "kind" | "simulated";
  context: string;
  server: string | null;
  namespace: string;
  reachable?: boolean;
  server_version?: string;
  nodes?: { name: string; ready: boolean; kubelet: string }[];
  error?: string;
  bootstrapped: boolean;
  bootstrap_error: string | null;
  control_loop: { running: boolean; tick_s: number; ticks: number; last_tick_at: string | null; last_tick_ms: number | null };
  load_generator: { running: boolean; serving: boolean; rps_per_service: number; cycle_period_s: number };
}

export interface StageEntry {
  stage: string;
  at: string;
  reason?: string;
  gate?: GateVerdict;
}

export interface Deployment {
  id: string;
  service_name: string;
  version: string;
  status: string;
  strategy: "CANARY" | "BLUE_GREEN" | null;
  config_diff: Record<string, unknown> | null;
  deployed_at: string | null;
  switched_at: string | null;
  finished_at: string | null;
  k8s_namespace: string | null;
  k8s_deployment_name: string | null;
  image: string | null;
  target_color: Color | null;
  stable_color: Color | null;
  auto_promote: boolean | null;
  stage_started_at: string | null;
  status_reason: string | null;
  stage_history: StageEntry[] | null;
  canary_weight: number;
}

export interface PodInfo {
  name: string;
  color: Color | null;
  version: string | null;
  phase: string;
  ready: boolean;
  restarts: number;
  waiting_reason: string | null;
  terminating: boolean;
  node: string | null;
}

export interface RolloutStatus {
  service: string;
  color: Color;
  version: string | null;
  desired: number;
  updated: number;
  ready: number;
  available: number;
  complete: boolean;
  failed: boolean;
  message: string;
  fatal_reason: string | null;
  pods: PodInfo[];
}

export interface K8sStatus {
  deployment_id: string;
  cluster_mode: string;
  namespace: string;
  live_color: Color | null;
  routing: { live_color: Color; canary_color: Color | null; canary_weight: number };
  candidate: RolloutStatus | { error: string } | null;
  stable: RolloutStatus | { error: string } | null;
  gate: GateVerdict | null;
}

export interface Incident {
  id: string;
  service_name: string;
  detected_at: string | null;
  resolved_at: string | null;
  status: "OPEN" | "INVESTIGATING" | "RESOLVED" | string;
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL" | null;
  trigger_reason: string;
  related_deployment_id: string | null;
  is_cascading: boolean | null;
  root_incident_id: string | null;
  blast_radius_services: string[] | null;
  chaos_experiment_id: string | null;
}

export interface MetricPoint {
  id: number;
  service_name: string;
  metric_name: MetricName;
  value: number;
  z_score: number | null;
  seasonal_residual_z: number | null;
  isolation_forest_score: number | null;
  recorded_at: string | null;
}

export interface LogEntry {
  id: number;
  service_name: string;
  level: string;
  message: string;
  error_signature: string | null;
  root_signature: string | null;
  occurrences: number;
  logged_at: string | null;
}

export interface IncidentDetail extends Incident {
  detection_context: {
    consensus?: Consensus | null;
    burn?: { worst: BurnSeverity | null; evaluations: BurnEvaluation[] } | null;
    events?: { at: string; reason: string }[];
    trigger?: string;
    canary_gate?: GateVerdict;
    resolution?: { at: string; note: string };
  } | null;
  timeline: { at: string; event: string }[];
  logs: LogEntry[];
  metrics: MetricPoint[];
}

export interface CascadeTree {
  root: Incident;
  children: Incident[];
  edges: { parent: string; child: string }[];
}

export interface RCACandidate {
  cause: string;
  confidence: "low" | "medium" | "high";
  evidence: string;
}

export interface RCAReport {
  id: string;
  incident_id: string;
  root_cause_candidates: RCACandidate[];
  llm_model_used: string | null;
  generated_at: string | null;
  signals: Record<string, unknown> | null;
}

export interface ImpactService {
  service: string;
  source: string;
  affected_requests: number;
  bad_requests?: number;
  incident_bad_ratio: number;
  baseline_bad_ratio: number;
  error_rate_delta: number;
  estimated_impact: number;
}

export interface BusinessImpact {
  id: string;
  incident_id: string;
  affected_requests: number | null;
  error_rate_delta: number | null;
  estimated_impact_value: number | null;
  calculation_basis: {
    formula: string;
    revenue_per_request: number;
    scope: string;
    per_service?: ImpactService[];
    note: string;
    window?: { start: string; end: string; open: boolean };
    [k: string]: unknown;
  } | null;
  calculated_at: string | null;
}

export interface BurnEvaluation {
  severity: BurnSeverity;
  long_window: string;
  short_window: string;
  threshold: number;
  long_burn: number | null;
  short_burn: number | null;
  long_requests: number;
  short_requests: number;
  coverage_s: number;
  firing: boolean;
  budget_fraction_at_threshold: number;
}

export interface BurnHistory {
  service: string;
  slo_target: number;
  step_s: number;
  thresholds: { severity: BurnSeverity; value: number; action: string }[];
  points: { t: string; burn_1h: number | null; burn_5m: number | null; requests_5m: number }[];
}

export interface SloBurn {
  service: string;
  slo_target: number;
  window_days: number;
  latency_threshold_ms: number;
  evaluations: BurnEvaluation[];
  worst: BurnSeverity | null;
  budget_consumed: number | null;
  budget_remaining: number | null;
  alerts: {
    severity: BurnSeverity;
    long_window: string;
    short_window: string;
    burn_rate: number;
    short_burn_rate: number | null;
    fired_at: string;
    resolved_at: string | null;
  }[];
}

export type FaultType = "LATENCY_INJECTION" | "ERROR_BURST" | "POD_KILL";

export interface FaultSpec {
  kind: string | null;
  default: number;
  min: number;
  max: number;
  unit: string;
}

export interface ChaosExperiment {
  id: string;
  service_name: string;
  fault_type: FaultType;
  magnitude: number | null;
  target: "all" | "stable" | "canary";
  status: "RUNNING" | "COMPLETED";
  injected_at: string | null;
  completed_at: string | null;
  duration_s: number | null;
  detection_latency_seconds: number | null;
  rollback_latency_seconds: number | null;
  recovery_latency_seconds: number | null;
  detection_bound_s: number | null;
  rollback_bound_s: number | null;
  passed: boolean | null;
  notes: string | null;
  deployment_id: string | null;
  affected_pods: { pod: string; color: Color; applied: boolean }[] | null;
  timeline: { at: string; t: number; event: string }[] | null;
}

// ---------------------------------------------------------------- calls --
export const api = {
  snapshot: () => get<Snapshot>("/console/snapshot"),
  cluster: () => get<ClusterInfo>("/cluster"),

  deployments: (service?: string) =>
    get<Deployment[]>(`/deployments${service ? `?service=${encodeURIComponent(service)}` : ""}`),
  deployment: (id: string) => get<Deployment>(`/deployments/${id}`),
  createDeployment: (body: {
    service_name: string;
    version: string;
    strategy: "CANARY" | "BLUE_GREEN";
    simulate_failure: boolean;
    auto_promote: boolean;
    image?: string;
    config_diff?: Record<string, unknown>;
  }) => post<Deployment>("/deployments", body),
  promote: (id: string) => post<{ action: string; verdict: GateVerdict; deployment: Deployment }>(`/deployments/${id}/promote-canary`),
  rollback: (id: string) => post<Deployment>(`/deployments/${id}/rollback`),
  k8sStatus: (id: string) => get<K8sStatus>(`/deployments/${id}/k8s-status`),

  incidents: (status?: string) => get<Incident[]>(`/incidents${status ? `?status=${status}` : ""}`),
  incident: (id: string) => get<IncidentDetail>(`/incidents/${id}`),
  cascadeTree: (id: string) => get<CascadeTree>(`/incidents/${id}/cascade-tree`),
  triggerRca: (id: string, blind = false) => post<RCAReport>(`/incidents/${id}/trigger-rca${blind ? "?blind=true" : ""}`),
  rcaReport: (id: string) => get<RCAReport>(`/incidents/${id}/rca-report`),
  impact: (id: string, refresh = false) =>
    get<BusinessImpact>(`/incidents/${id}/business-impact${refresh ? "?refresh=true" : ""}`),
  resolve: (id: string) => post<Incident>(`/incidents/${id}/resolve`),

  sloBurn: (service: string) => get<SloBurn>(`/services/${service}/slo-burn-rate`),
  burnHistory: (service: string, minutes = 60, stepS = 30) =>
    get<BurnHistory>(`/services/${service}/burn-rate-history?minutes=${minutes}&step_s=${stepS}`),
  metrics: (service: string, window = "15m") => get<MetricPoint[]>(`/metrics/${service}?window=${window}`),

  faultTypes: () => get<Record<FaultType, FaultSpec>>("/chaos/fault-types"),
  experiments: () => get<ChaosExperiment[]>("/chaos/experiments"),
  experiment: (id: string) => get<ChaosExperiment>(`/chaos/experiments/${id}`),
  inject: (body: {
    service_name: string;
    fault_type: FaultType;
    magnitude?: number;
    target: "all" | "stable" | "canary";
    duration_s?: number;
  }) => post<ChaosExperiment>("/chaos/inject", body),
  abort: (id: string) => post<ChaosExperiment>(`/chaos/experiments/${id}/abort`),
};

// The backend stores naive UTC and serialises it without an offset;
// new Date() would read such a string as local time.
export function parseUtc(value: string | null | undefined): Date | null {
  if (!value) return null;
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "verdict" in d) return String((d as { verdict: GateVerdict }).verdict.reason);
    if (Array.isArray(d)) return d.map((x) => x?.msg ?? String(x)).join("; ");
    return `Request failed (${err.status})`;
  }
  return err instanceof Error ? err.message : String(err);
}
