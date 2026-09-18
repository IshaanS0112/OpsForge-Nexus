import { useMemo, useState } from "react";
import { Link } from "react-router";
import {
  api,
  parseUtc,
  type BusinessImpact,
  type CascadeTree,
  type Consensus,
  type Incident,
  type LayerDetail,
  type MetricName,
  type RCAReport,
} from "../api/client";
import { fmtBurn, fmtCompact, fmtDateTime, fmtMetric, fmtMoney, fmtPct, METRIC_LABEL } from "../lib/format";
import { usePoll } from "../lib/hooks";
import { incidentTone, LAYER_LABEL, type Tone } from "../lib/status";
import { BurnRateChart, Sparkline } from "./charts";
import { Empty, ErrorNote, StatusBadge, StatusIcon } from "./ui";

// ------------------------------------------------------------ consensus --
function layerEvidence(l: LayerDetail): string {
  if (!l.warm) {
    if (l.training) return `training on ${l.training}`;
    if (l.warmup) {
      const first = Object.entries(l.warmup)[0];
      return first ? `warming up: ${first[1]} of history` : "warming up";
    }
    return "warming up: needs a baseline";
  }
  if (l.layer === "isolation_forest") {
    return `score ${l.score?.toFixed(3)} vs threshold ${l.threshold?.toFixed(3)}`;
  }
  const zs = (l.layer === "zscore" ? l.z : l.residual_z) ?? {};
  const top = Object.entries(zs).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0))[0];
  if (!top) return "no readings";
  const name = METRIC_LABEL[top[0]]?.toLowerCase() ?? top[0];
  return `${l.layer === "zscore" ? "z" : "residual z"} ${top[1]?.toFixed(1)} on ${name}`;
}

/** The three detector layers side by side: what each saw, whether it is warm. */
export function ConsensusLayers({ consensus }: { consensus: Consensus | null | undefined }) {
  if (!consensus) return <Empty>No readings yet for this service.</Empty>;
  const layers = [consensus.layers.zscore, consensus.layers.seasonal, consensus.layers.isolation_forest];
  const agree = consensus.agreeing_layers;
  return (
    <div>
      <ul className="divide-y divide-[var(--ring)]">
        {layers.map((l) => {
          const tone: Tone = !l.warm ? "neutral" : l.flagged ? (agree >= 2 ? "critical" : "warn") : "ok";
          return (
            <li key={l.layer} className="flex items-center gap-3 py-2">
              <StatusIcon tone={tone} size={14} />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-semibold">{LAYER_LABEL[l.layer]}</div>
                <div className="mono truncate text-[11px] text-text-2">{layerEvidence(l)}</div>
              </div>
              <span className="text-[12px] font-semibold text-text-2">{!l.warm ? "warming" : l.flagged ? "flagged" : "normal"}</span>
            </li>
          );
        })}
      </ul>
      <div className="mt-2 flex items-center gap-2 rounded-lg bg-panel-2 px-3 py-2 text-[12px]">
        <span className="mono text-[15px] font-bold">{agree}/3</span>
        <span className="text-text-2">
          {agree >= 2
            ? "layers agree: auto-action (incident, rollback-eligible)"
            : agree === 1
              ? "one layer flagging: surfaced here, not acted on"
              : "no layer flagging"}
        </span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------ sparklines --
const ORDER: MetricName[] = ["latency_p95", "error_rate", "cpu_usage", "memory_usage"];

export function MetricSparklines({
  service,
  window = "15m",
  tone = "neutral",
  latencyThresholdMs,
}: {
  service: string;
  window?: string;
  tone?: Tone;
  latencyThresholdMs?: number;
}) {
  const { data, error } = usePoll(() => api.metrics(service, window), 5000, [service, window]);
  const series = useMemo(() => {
    const out: Record<string, { t: Date; v: number }[]> = {};
    for (const m of [...(data ?? [])].reverse()) {
      const t = parseUtc(m.recorded_at);
      if (t) (out[m.metric_name] ??= []).push({ t, v: m.value });
    }
    return out;
  }, [data]);
  if (error) return <ErrorNote>Could not load metrics.</ErrorNote>;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {ORDER.map((metric) => {
        const pts = series[metric] ?? [];
        const last = pts[pts.length - 1];
        return (
          <div key={metric} className="min-w-0 rounded-lg bg-panel-2 px-3 py-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[12px] text-text-2">{METRIC_LABEL[metric]}</span>
              <span className="mono text-[13px] font-semibold">{last ? fmtMetric(metric, last.v) : "—"}</span>
            </div>
            <Sparkline
              points={pts}
              label={`${METRIC_LABEL[metric]} for ${service}`}
              format={(v) => fmtMetric(metric, v)}
              tone={tone}
              threshold={metric === "latency_p95" ? latencyThresholdMs : undefined}
            />
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------ burn panel --
export function BurnPanel({ service, minutes = 30 }: { service: string; minutes?: number }) {
  const hist = usePoll(() => api.burnHistory(service, minutes, minutes > 60 ? 60 : 15), 10000, [service, minutes]);
  const slo = usePoll(() => api.sloBurn(service), 10000, [service]);
  const points = useMemo(
    () =>
      (hist.data?.points ?? []).map((p) => ({ t: parseUtc(p.t)!, long: p.burn_1h, short: p.burn_5m })),
    [hist.data],
  );
  const budget = slo.data?.budget_remaining;
  return (
    <div className="space-y-3">
      <BurnRateChart points={points} />
      {slo.data && (
        <>
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="py-1 font-medium">Alert</th>
                <th className="py-1 font-medium">Windows</th>
                <th className="py-1 text-right font-medium">Long / short</th>
                <th className="py-1 text-right font-medium">Threshold</th>
                <th className="py-1 text-right font-medium">State</th>
              </tr>
            </thead>
            <tbody className="num">
              {slo.data.evaluations.map((e) => (
                <tr key={e.severity} className="border-t border-[var(--ring)]">
                  <td className="py-1.5 font-semibold">{e.severity.replace("_", " ").toLowerCase()}</td>
                  <td className="mono py-1.5 text-text-2">
                    {e.long_window} + {e.short_window}
                  </td>
                  <td className="mono py-1.5 text-right">
                    {fmtBurn(e.long_burn)} / {fmtBurn(e.short_burn)}
                  </td>
                  <td className="mono py-1.5 text-right text-text-2">{fmtBurn(e.threshold)}</td>
                  <td className="py-1.5 text-right">
                    {e.firing ? (
                      <StatusBadge tone={e.severity === "FAST_BURN" ? "critical" : "warn"} label="firing" />
                    ) : (
                      <span className="text-muted">quiet</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center gap-3 text-[12px]">
            <span className="text-text-2">
              SLO <span className="mono">{(slo.data.slo_target * 100).toFixed(2)}%</span> over {slo.data.window_days}d · slow
              request &gt; <span className="mono">{slo.data.latency_threshold_ms} ms</span>
            </span>
            {budget != null && (
              <span className="ml-auto inline-flex items-center gap-2">
                <span className="text-text-2">budget left</span>
                <span className="inline-block h-1.5 w-20 overflow-hidden rounded-full bg-grid" aria-hidden>
                  <span
                    className="block h-full"
                    style={{
                      width: `${Math.max(0, Math.min(100, budget * 100))}%`,
                      background: budget < 0.25 ? "var(--critical)" : budget < 0.6 ? "var(--warn)" : "var(--ok)",
                    }}
                  />
                </span>
                <span className="mono font-semibold">{fmtPct(Math.max(0, budget), 1)}</span>
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------- cascade tree --
export function CascadeTreeView({ tree, current }: { tree: CascadeTree; current?: string }) {
  const children = useMemo(() => {
    const byService = new Map(tree.children.map((c) => [c.service_name, c]));
    const map = new Map<string, Incident[]>();
    for (const e of tree.edges) {
      const child = byService.get(e.child);
      if (child && !(map.get(e.parent) ?? []).includes(child)) map.set(e.parent, [...(map.get(e.parent) ?? []), child]);
    }
    return map;
  }, [tree]);
  const t0 = parseUtc(tree.root.detected_at)?.getTime() ?? 0;
  const seen = new Set<string>();

  const renderRow = (inc: Incident, depth: number) => {
    if (seen.has(inc.id)) return null;
    seen.add(inc.id);
    const dt = ((parseUtc(inc.detected_at)?.getTime() ?? t0) - t0) / 1000;
    return (
      <li key={inc.id}>
        <div className="flex items-center gap-2 py-1" style={{ paddingLeft: depth * 18 }}>
          {depth > 0 && (
            <svg width="12" height="12" aria-hidden className="shrink-0">
              <path d="M1 0v6h10" fill="none" stroke="var(--axis)" strokeWidth="1.5" />
            </svg>
          )}
          <StatusIcon tone={incidentTone(inc)} size={13} />
          <Link to={`/incidents/${inc.id}`} className={`mono text-[13px] hover:underline ${inc.id === current ? "font-bold" : ""}`}>
            {inc.service_name}
          </Link>
          <span className="text-[11px] text-muted">{depth === 0 ? "root" : `+${Math.round(dt)}s`}</span>
          {inc.status === "RESOLVED" && <span className="text-[11px] text-muted">resolved</span>}
        </div>
        <ul>{(children.get(inc.service_name) ?? []).map((c) => renderRow(c, depth + 1))}</ul>
      </li>
    );
  };
  return (
    <ul aria-label="Cascade tree">{renderRow(tree.root, 0)}</ul>
  );
}

// ------------------------------------------------------------------ RCA --
const CONF_LEVEL: Record<string, number> = { low: 1, medium: 2, high: 3 };

/** Confidence is not a status, so it gets no status colour: a neutral meter. */
function Confidence({ level }: { level: string }) {
  const n = CONF_LEVEL[level] ?? 0;
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-[11px] font-semibold text-text-2" title="Model confidence">
      <span className="inline-flex items-end gap-0.5" aria-hidden>
        {[1, 2, 3].map((i) => (
          <span key={i} className="w-1 rounded-sm" style={{ height: 4 + i * 3, background: i <= n ? "var(--data)" : "var(--grid)" }} />
        ))}
      </span>
      {level}
    </span>
  );
}

export function RcaReportView({ report }: { report: RCAReport }) {
  const [showSignals, setShowSignals] = useState(false);
  const fallback = report.llm_model_used === "rule-based-fallback";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px] text-text-2">
        {fallback ? (
          <StatusBadge tone="warn" label="rule-based fallback" title="The LLM was unavailable or declined; the same signals were ranked by rules." />
        ) : (
          <span className="mono">{report.llm_model_used}</span>
        )}
        <span>· {fmtDateTime(parseUtc(report.generated_at))}</span>
      </div>
      <ol className="space-y-2.5">
        {report.root_cause_candidates.map((c, i) => (
          <li key={i} className="rounded-lg bg-panel-2 px-3 py-2.5">
            <div className="flex items-start gap-2">
              <span className="mono mt-0.5 text-[12px] font-bold text-muted">#{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-semibold">{c.cause}</div>
                <div className="mono mt-1 break-words text-[12px] text-text-2">{c.evidence}</div>
              </div>
              <Confidence level={c.confidence} />
            </div>
          </li>
        ))}
      </ol>
      {report.signals && (
        <div>
          <button type="button" className="text-[12px] font-semibold text-text-2 hover:text-text" onClick={() => setShowSignals((v) => !v)} aria-expanded={showSignals}>
            {showSignals ? "Hide" : "Show"} the structured context the ranking was computed over
          </button>
          {showSignals && (
            <pre className="mono mt-2 max-h-80 overflow-auto rounded-lg bg-panel-2 p-3 text-[11px] leading-relaxed text-text-2">
              {JSON.stringify(report.signals, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------- impact --
export function ImpactView({ impact }: { impact: BusinessImpact }) {
  const basis = impact.calculation_basis;
  const rows = basis?.per_service ?? [];
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className="text-[12px] text-muted">Estimated impact</div>
          <div className="mt-0.5 text-[22px] font-bold">{fmtMoney(impact.estimated_impact_value)}</div>
        </div>
        <div>
          <div className="text-[12px] text-muted">Requests in window</div>
          <div className="mono mt-0.5 text-[18px] font-semibold">{fmtCompact(impact.affected_requests)}</div>
        </div>
        <div>
          <div className="text-[12px] text-muted">Excess bad ratio</div>
          <div className="mono mt-0.5 text-[18px] font-semibold">{fmtPct(impact.error_rate_delta, 2)}</div>
        </div>
      </div>
      {rows.length > 0 && (
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-left text-muted">
              <th className="py-1 font-medium">Service</th>
              <th className="py-1 text-right font-medium">Requests</th>
              <th className="py-1 text-right font-medium">Bad ratio</th>
              <th className="py-1 text-right font-medium">Baseline</th>
              <th className="py-1 text-right font-medium">Impact</th>
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => (
              <tr key={r.service} className="border-t border-[var(--ring)]">
                <td className="mono py-1.5">{r.service}</td>
                <td className="mono py-1.5 text-right">{fmtCompact(r.affected_requests)}</td>
                <td className="mono py-1.5 text-right">{fmtPct(r.incident_bad_ratio, 2)}</td>
                <td className="mono py-1.5 text-right text-text-2">{fmtPct(r.baseline_bad_ratio, 2)}</td>
                <td className="mono py-1.5 text-right font-semibold">{fmtMoney(r.estimated_impact)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {basis && (
        <p className="text-[12px] text-text-2">
          <span className="mono">{basis.formula}</span> at <span className="mono">{fmtMoney(basis.revenue_per_request)}</span>/request ·{" "}
          {basis.scope}. {basis.note}.
        </p>
      )}
    </div>
  );
}
