import axios from "axios";

// In dev, Vite proxies "/api" -> backend. In Docker, set VITE_API_BASE.
const baseURL = import.meta.env.VITE_API_BASE || "/api";

export const api = axios.create({ baseURL, timeout: 30000 });

// ---------- Types (mirror the backend Pydantic schemas) ----------
export interface Deployment {
  id: string;
  service_name: string;
  version: string;
  status: string;
  config_diff?: Record<string, unknown> | null;
  deployed_at?: string | null;
  switched_at?: string | null;
}

export interface Incident {
  id: string;
  service_name: string;
  status: string;
  severity?: string | null;
  trigger_reason: string;
  detected_at?: string | null;
  resolved_at?: string | null;
  related_deployment_id?: string | null;
}

export interface Metric {
  id: number;
  service_name: string;
  metric_name: string;
  value: number;
  z_score?: number | null;
  recorded_at?: string | null;
}

export interface LogEntry {
  id: number;
  service_name: string;
  level: string;
  message: string;
  error_signature?: string | null;
  logged_at?: string | null;
}

export interface IncidentDetail extends Incident {
  timeline: { at: string; event: string }[];
  logs: LogEntry[];
  metrics: Metric[];
}

export interface RootCauseCandidate {
  cause: string;
  confidence: string;
  evidence: string;
}

export interface RCAReport {
  id: string;
  incident_id: string;
  root_cause_candidates: RootCauseCandidate[];
  llm_model_used?: string | null;
  generated_at?: string | null;
}

export interface BusinessImpact {
  id: string;
  incident_id: string;
  affected_requests?: number | null;
  error_rate_delta?: number | null;
  estimated_impact_value?: number | null;
  calculation_basis?: Record<string, unknown> | null;
  calculated_at?: string | null;
}

// ---------- API calls ----------
export const listDeployments = () => api.get<Deployment[]>("/deployments").then((r) => r.data);
export const getDeployment = (id: string) =>
  api.get<Deployment>(`/deployments/${id}`).then((r) => r.data);
export const createDeployment = (body: {
  service_name: string;
  version: string;
  simulate_failure: boolean;
}) => api.post<Deployment>("/deployments", body).then((r) => r.data);
export const rollbackDeployment = (id: string) =>
  api.post<Deployment>(`/deployments/${id}/rollback`).then((r) => r.data);

export const listIncidents = () => api.get<Incident[]>("/incidents").then((r) => r.data);
export const getIncident = (id: string) =>
  api.get<IncidentDetail>(`/incidents/${id}`).then((r) => r.data);
export const triggerRCA = (id: string) =>
  api.post<RCAReport>(`/incidents/${id}/trigger-rca`).then((r) => r.data);
export const getRCAReport = (id: string) =>
  api.get<RCAReport>(`/incidents/${id}/rca-report`).then((r) => r.data);
export const getBusinessImpact = (id: string) =>
  api.get<BusinessImpact>(`/incidents/${id}/business-impact`).then((r) => r.data);
