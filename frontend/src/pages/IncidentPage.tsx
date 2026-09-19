import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { api, ApiError, errorMessage, parseUtc } from "../api/client";
import { BurnPanel, CascadeTreeView, ConsensusLayers, ImpactView, RcaReportView } from "../components/detail";
import { Sparkline } from "../components/charts";
import { Empty, ErrorNote, Panel, StatusBadge } from "../components/ui";
import { fmtClock, fmtDateTime, fmtMetric, fmtSeconds, METRIC_LABEL } from "../lib/format";
import { useNow, usePoll } from "../lib/hooks";
import { incidentTone } from "../lib/status";

const notFoundAsNull = <T,>(p: Promise<T>) =>
  p.catch((e) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e)));

export default function IncidentPage() {
  const { id = "" } = useParams();
  const now = useNow(5000);
  const incident = usePoll(() => api.incident(id), 5000, [id]);
  const tree = usePoll(() => api.cascadeTree(id), 10000, [id]);
  const rca = usePoll(() => notFoundAsNull(api.rcaReport(id)), 0, [id]);
  const impact = usePoll(() => api.impact(id, true), 0, [id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const inc = incident.data;

  const series = useMemo(() => {
    const out: Record<string, { t: Date; v: number }[]> = {};
    for (const m of [...(inc?.metrics ?? [])].reverse()) {
      const t = parseUtc(m.recorded_at);
      if (t) (out[m.metric_name] ??= []).push({ t, v: m.value });
    }
    return out;
  }, [inc]);

  const signatures = useMemo(() => {
    const agg = new Map<string, { signature: string; root: string | null; count: number }>();
    for (const l of inc?.logs ?? []) {
      if (!l.error_signature) continue;
      const key = `${l.error_signature}|${l.root_signature ?? ""}`;
      const cur = agg.get(key) ?? { signature: l.error_signature, root: l.root_signature, count: 0 };
      cur.count += l.occurrences;
      agg.set(key, cur);
    }
    return [...agg.values()].sort((a, b) => b.count - a.count);
  }, [inc]);

  async function act(name: string, fn: () => Promise<unknown>) {
    setBusy(name);
    setActionError(null);
    try {
      await fn();
    } catch (e) {
      setActionError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  if (!inc) {
    return incident.error ? <ErrorNote>{errorMessage(incident.error)}</ErrorNote> : <div className="py-24 text-center text-muted">Loading incident…</div>;
  }

  const drillLinked = Boolean(inc.chaos_experiment_id || tree.data?.root.chaos_experiment_id);
  const detected = parseUtc(inc.detected_at);
  const resolved = parseUtc(inc.resolved_at);
  const duration = detected ? ((resolved ?? now).getTime() - detected.getTime()) / 1000 : null;
  const ctx = inc.detection_context;

  return (
    <div className="space-y-4 pb-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to="/incidents" className="text-[12px] font-semibold text-text-2 hover:text-text">
            ← Incidents
          </Link>
          <h1 className="mt-1 flex flex-wrap items-center gap-3 text-[22px] font-bold tracking-tight">
            <span className="mono">{inc.service_name}</span>
            <StatusBadge tone={incidentTone(inc)} label={inc.severity ?? inc.status} />
            {inc.is_cascading && <span className="text-[13px] font-semibold text-text-2">cascading</span>}
          </h1>
          <p className="mt-1 max-w-3xl text-[14px] text-text-2">{inc.trigger_reason}</p>
          <p className="mt-1 text-[12px] text-muted">
            {inc.status.toLowerCase()} · detected {fmtDateTime(detected)} · {resolved ? "lasted" : "open for"} {fmtSeconds(duration)}
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn" disabled={busy != null} onClick={() => act("rca", async () => { await api.triggerRca(id); await rca.refresh(); await impact.refresh(); })}>
            {busy === "rca" ? "Analysing…" : rca.data ? "Re-run RCA" : "Run RCA"}
          </button>
          {drillLinked && (
            <button
              className="btn"
              disabled={busy != null}
              title="Rank causes with the chaos drill withheld, to test whether RCA finds the fault from the evidence alone"
              onClick={() => act("blind", async () => { await api.triggerRca(id, true); await rca.refresh(); })}
            >
              {busy === "blind" ? "Analysing…" : "Blind RCA"}
            </button>
          )}
          {inc.status !== "RESOLVED" && (
            <button className="btn" disabled={busy != null} onClick={() => act("resolve", async () => { await api.resolve(id); await incident.refresh(); })}>
              Resolve
            </button>
          )}
        </div>
      </div>
      {actionError && <ErrorNote>{actionError}</ErrorNote>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <div className="space-y-4">
          <Panel eyebrow="Root-cause analysis" title="Ranked candidates, with the evidence each cites">
            {rca.data ? <RcaReportView report={rca.data} /> : <Empty>No report yet. Run RCA to rank causes over the collected signals.</Empty>}
          </Panel>
          <Panel eyebrow="SLO" title="Burn rate around the incident">
            <BurnPanel service={inc.service_name} minutes={60} />
          </Panel>
          <Panel eyebrow="Telemetry" title="Metrics in the lookback window">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {(["latency_p95", "error_rate", "cpu_usage", "memory_usage"] as const).map((m) => {
                const pts = series[m] ?? [];
                const last = pts[pts.length - 1];
                return (
                  <div key={m} className="min-w-0 rounded-lg bg-panel-2 px-3 py-2">
                    <div className="flex items-baseline justify-between">
                      <span className="text-[12px] text-text-2">{METRIC_LABEL[m]}</span>
                      <span className="mono text-[13px] font-semibold">{last ? fmtMetric(m, last.v) : "—"}</span>
                    </div>
                    <Sparkline points={pts} label={`${METRIC_LABEL[m]} for ${inc.service_name}`} format={(v) => fmtMetric(m, v)} />
                  </div>
                );
              })}
            </div>
          </Panel>
        </div>
        <div className="space-y-4">
          {tree.data && (
            <Panel eyebrow="Dependency context" title={tree.data.children.length ? "Cascade tree" : "No cascade"}>
              <CascadeTreeView tree={tree.data} current={id} />
              {inc.blast_radius_services && inc.blast_radius_services.length > 0 && !inc.is_cascading && (
                <p className="mt-3 text-[12px] text-text-2">
                  Blast radius: <span className="mono">{inc.blast_radius_services.join(", ")}</span>
                </p>
              )}
            </Panel>
          )}
          <Panel eyebrow="Business impact" title="What it cost">
            {impact.data ? <ImpactView impact={impact.data} /> : impact.error ? <ErrorNote>{errorMessage(impact.error)}</ErrorNote> : <div className="text-muted">Calculating…</div>}
          </Panel>
          <Panel eyebrow="At detection" title="Detector layers">
            <ConsensusLayers consensus={ctx?.consensus} />
          </Panel>
          <Panel eyebrow="Timeline" title="What happened, in order">
            <ol className="space-y-2">
              {inc.timeline.map((e, i) => (
                <li key={i} className="flex gap-3 text-[12px]">
                  <span className="mono shrink-0 text-muted">{fmtClock(parseUtc(e.at))}</span>
                  <span className="min-w-0 break-words">{e.event}</span>
                </li>
              ))}
            </ol>
          </Panel>
          <Panel eyebrow="Logs" title="Error signatures">
            {signatures.length === 0 ? (
              <Empty>No error signatures in the window.</Empty>
            ) : (
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="text-left text-muted">
                    <th className="py-1 font-medium">Signature</th>
                    <th className="py-1 text-right font-medium">Count</th>
                  </tr>
                </thead>
                <tbody className="num">
                  {signatures.slice(0, 8).map((s) => (
                    <tr key={`${s.signature}|${s.root}`} className="border-t border-[var(--ring)] align-top">
                      <td className="py-1.5">
                        <div className="mono break-all">{s.signature}</div>
                        {s.root && <div className="mono break-all text-muted">caused by {s.root}</div>}
                      </td>
                      <td className="mono py-1.5 text-right">{s.count.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
