// Formatting for figures shown in the console. Units are always explicit.

export const dash = "—";

export function fmtPct(x: number | null | undefined, digits = 1): string {
  if (x == null || Number.isNaN(x)) return dash;
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtMs(x: number | null | undefined): string {
  if (x == null || Number.isNaN(x)) return dash;
  if (x >= 1000) return `${(x / 1000).toFixed(x >= 10000 ? 0 : 2)} s`;
  return `${x < 10 ? x.toFixed(1) : Math.round(x)} ms`;
}

/** Burn rate as a multiple of the sustainable rate: 0.42×, 14.4×, 312×. */
export function fmtBurn(x: number | null | undefined): string {
  if (x == null || Number.isNaN(x)) return dash;
  if (x < 10) return `${x.toFixed(2)}×`;
  if (x < 100) return `${x.toFixed(1)}×`;
  return `${Math.round(x).toLocaleString()}×`;
}

export function fmtCompact(n: number | null | undefined): string {
  if (n == null) return dash;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return Math.round(n).toLocaleString();
}

export function fmtMoney(n: number | null | undefined): string {
  if (n == null) return dash;
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

export function fmtSeconds(s: number | null | undefined): string {
  if (s == null) return dash;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return `${m}m ${String(rest).padStart(2, "0")}s`;
}

export function fmtAgo(date: Date | null, now: Date = new Date()): string {
  if (!date) return dash;
  const s = Math.max(0, (now.getTime() - date.getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function fmtClock(date: Date | null): string {
  if (!date) return dash;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(date: Date | null): string {
  if (!date) return dash;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const METRIC_LABEL: Record<string, string> = {
  error_rate: "Error rate",
  latency_p95: "Latency p95",
  cpu_usage: "CPU",
  memory_usage: "Memory",
};

export function fmtMetric(metric: string, v: number | null | undefined): string {
  if (metric === "latency_p95") return fmtMs(v);
  return fmtPct(v, metric === "error_rate" ? 2 : 0);
}
