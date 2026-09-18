import type { Status } from "../api/client";

// The status vocabulary: every colour means exactly one thing, and always
// travels with an icon and a label.
export const STATUS_META: Record<Status, { label: string; hint: string }> = {
  ok: { label: "Healthy", hint: "Operating within its SLO" },
  warn: { label: "Degraded", hint: "One detector layer flagging, or a slow/medium burn" },
  critical: { label: "Incident", hint: "Two detector layers agree, or a FAST_BURN page" },
  rollback: { label: "Rolling back", hint: "The safety net is correcting" },
  canary: { label: "Canary", hint: "A canary stage is in progress" },
};

export const statusColor = (s: Status) => `var(--${s})`;

/** A tone for things outside the five statuses (in-flight steps, labels). */
export type Tone = Status | "neutral";

export function incidentTone(inc: { status: string; severity: string | null }): Tone {
  if (inc.status === "RESOLVED") return "ok";
  return inc.severity === "CRITICAL" || inc.severity === "HIGH" ? "critical" : "warn";
}

export function deploymentTone(status: string): Tone {
  if (status === "LIVE") return "ok";
  if (status === "ROLLED_BACK" || status === "ROLLING_BACK") return "rollback";
  if (status.startsWith("CANARY_")) return "canary";
  return "neutral";
}

export const DEPLOY_LABEL: Record<string, string> = {
  IDLE: "Queued",
  PREPARING_GREEN: "Preparing",
  DEPLOYING_GREEN: "Rolling out pods",
  HEALTH_CHECKING: "Health gate",
  CANARY_5: "Canary 5%",
  CANARY_25: "Canary 25%",
  CANARY_50: "Canary 50%",
  CANARY_100: "Canary 100%",
  TRAFFIC_SWITCHING: "Switching traffic",
  LIVE: "Live",
  ROLLING_BACK: "Rolling back",
  ROLLED_BACK: "Rolled back",
};

export const FAULT_LABEL: Record<string, string> = {
  LATENCY_INJECTION: "Latency injection",
  ERROR_BURST: "Error burst",
  POD_KILL: "Pod kill",
};

export const LAYER_LABEL: Record<string, string> = {
  zscore: "Z-score",
  seasonal: "Seasonal (STL)",
  isolation_forest: "Isolation Forest",
};
