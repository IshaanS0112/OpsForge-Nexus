import { useMemo, useState } from "react";
import { Link } from "react-router";
import { api, ApiError, errorMessage, parseUtc, type ServiceState, type Snapshot } from "../api/client";
import { CanaryRail, ServiceRail, StatusStrip } from "../components/console";
import { DependencyGraph, GraphLegend } from "../components/DependencyGraph";
import { BurnPanel, CascadeTreeView, ConsensusLayers, MetricSparklines, RcaReportView } from "../components/detail";
import { Empty, ErrorNote, Panel, StatusBadge } from "../components/ui";
import { fmtAgo, fmtBurn } from "../lib/format";
import { blastRadius } from "../lib/graph";
import { useNow, usePoll } from "../lib/hooks";
import { DEPLOY_LABEL, incidentTone } from "../lib/status";

export default function ConsolePage() {
  const { data: snap, error } = usePoll(api.snapshot, 2000);
  const [selected, setSelected] = useState<string | null>(null);
  const [blastMode, setBlastMode] = useState(false);

  if (!snap) {
    return error ? (
      <ErrorNote>Cannot reach the OpsForge API: {errorMessage(error)}. Is the backend running on :8000?</ErrorNote>
    ) : (
      <div className="py-24 text-center text-muted">Connecting to the control loop…</div>
    );
  }

  const selectedState = snap.services.find((s) => s.name === selected) ?? null;
  const rollouts = snap.services.filter((s) => s.deployment);
  const drillServices = new Set(snap.drills.map((d) => d.service_name));

  return (
    <div className="space-y-4 pb-4">
      <StatusStrip snap={snap} />
      {error ? <ErrorNote>Live updates paused: {errorMessage(error)}. Showing the last snapshot.</ErrorNote> : null}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[232px_minmax(0,1fr)] 2xl:grid-cols-[232px_minmax(0,1fr)_400px]">
        <ServiceRail services={snap.services} selected={selected} onSelect={setSelected} />
        <Panel
          eyebrow="Dependency graph"
          title={blastMode && selected ? `Blast radius of ${selected}` : "Services and what they call"}
          actions={
            <label className="flex cursor-pointer select-none items-center gap-2 text-[12px] font-semibold text-text-2">
              <input type="checkbox" className="h-3.5 w-3.5 accent-[var(--text-2)]" checked={blastMode} onChange={(e) => setBlastMode(e.target.checked)} />
              Blast radius
            </label>
          }
        >
          <DependencyGraph
            services={snap.services}
            edges={snap.edges}
            selected={selected}
            onSelect={setSelected}
            blastMode={blastMode}
            drillServices={drillServices}
          />
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--ring)] pt-3">
            <GraphLegend />
            <span className="text-[12px] text-muted">
              {blastMode ? (selected ? "Dimmed services cannot be hurt by it." : "Select a service to see what it can hurt.") : "Select a service for detail."}
            </span>
          </div>
        </Panel>
        <div className="lg:col-span-2 2xl:col-span-1">
          <Drawer snap={snap} service={selectedState} onSelect={setSelected} />
        </div>
      </div>
      {rollouts.length > 0 && (
        <div className="sticky bottom-3 z-20 space-y-2">
          {rollouts.map((s) => (
            <div key={s.name} className="panel px-4 py-3" style={{ borderColor: "color-mix(in oklab, var(--canary) 45%, transparent)" }}>
              <CanaryRail service={s} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Drawer({ snap, service, onSelect }: { snap: Snapshot; service: ServiceState | null; onSelect: (s: string | null) => void }) {
  if (service?.incident) return <IncidentPeek key={service.incident.id} incidentId={service.incident.id} service={service} />;
  if (service) return <ServicePeek service={service} snap={snap} />;
  return <OpenIncidents snap={snap} onSelect={onSelect} />;
}

function OpenIncidents({ snap, onSelect }: { snap: Snapshot; onSelect: (s: string) => void }) {
  const active = snap.services.filter((s) => s.incident);
  const roots = active.filter((s) => !s.incident!.is_cascading);
  // Incidents on services the graph does not draw (external telemetry, retired services).
  const elsewhere = Math.max(0, snap.summary.open_incidents - active.length);
  return (
    <Panel
      eyebrow="On call"
      title={active.length ? `${active.length} open incident${active.length > 1 ? "s" : ""}` : elsewhere ? "Graph quiet" : "All quiet"}
    >
      {elsewhere > 0 && (
        <p className="mb-3 text-[12px] text-text-2">
          {elsewhere} more on services outside the graph.{" "}
          <Link to="/incidents" className="font-semibold underline-offset-2 hover:text-text hover:underline">
            See all incidents →
          </Link>
        </p>
      )}
      {active.length === 0 ? (
        <Empty>
          No open incidents on these services. Run a chaos drill or a failing rollout to see detection, cascades and rollback here.
        </Empty>
      ) : (
        <ul className="space-y-2">
          {active
            .sort((a, b) => Number(a.incident!.is_cascading) - Number(b.incident!.is_cascading))
            .map((s) => (
              <li key={s.name}>
                <button
                  type="button"
                  onClick={() => onSelect(s.name)}
                  className="flex w-full items-center gap-3 rounded-lg bg-panel-2 px-3 py-2 text-left hover:ring-1 hover:ring-[var(--axis)]"
                >
                  <StatusBadge tone={incidentTone({ status: s.incident!.status, severity: s.incident!.severity })} label={s.incident!.severity ?? "OPEN"} />
                  <span className="mono min-w-0 flex-1 truncate text-[13px] font-semibold">{s.name}</span>
                  <span className="text-[12px] text-text-2">{s.incident!.is_cascading ? "cascading" : roots.length > 1 ? "root" : "root cause"}</span>
                </button>
              </li>
            ))}
        </ul>
      )}
      <p className="mt-3 text-[12px] text-muted">
        One root failure opens one root incident; services it drags down are filed under it as cascading, not as new pages.
      </p>
    </Panel>
  );
}

function ServicePeek({ service, snap }: { service: ServiceState; snap: Snapshot }) {
  const radius = useMemo(() => blastRadius(service.name, snap.edges), [service.name, snap.edges]);
  return (
    <div className="space-y-4">
      <Panel
        eyebrow="Service"
        title={<span className="mono">{service.name}</span>}
        actions={<StatusBadge tone={service.status} />}
      >
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[12px]">
          <dt className="text-muted">Live version</dt>
          <dd className="mono text-right">{service.live_version} · {service.route.live_color}</dd>
          <dt className="text-muted">Traffic</dt>
          <dd className="mono text-right">{service.traffic ? `${service.traffic.rps} rps` : "—"}</dd>
          <dt className="text-muted">1h / 5m burn</dt>
          <dd className="mono text-right">
            {fmtBurn(service.burn?.fast_long)} / {fmtBurn(service.burn?.fast_short)}
          </dd>
          <dt className="text-muted">Calls</dt>
          <dd className="mono text-right">{service.upstream.join(", ") || "nothing"}</dd>
          <dt className="text-muted">Blast radius</dt>
          <dd className="mono text-right">{radius.length ? radius.join(", ") : "none"}</dd>
        </dl>
        {service.deployment && (
          <div className="mt-3 rounded-lg bg-panel-2 px-3 py-2 text-[12px]">
            <span className="font-semibold">{DEPLOY_LABEL[service.deployment.status]}</span>{" "}
            <span className="mono text-text-2">{service.deployment.version}</span>
            <div className="mt-0.5 text-text-2">{service.deployment.status_reason}</div>
          </div>
        )}
      </Panel>
      <Panel eyebrow="Anomaly suite" title="Detector layers">
        <ConsensusLayers consensus={service.consensus} />
      </Panel>
      <Panel eyebrow="Last 15 minutes" title="Metrics">
        <MetricSparklines service={service.name} tone={service.status} latencyThresholdMs={service.burn?.latency_threshold_ms} />
      </Panel>
    </div>
  );
}

function IncidentPeek({ incidentId, service }: { incidentId: string; service: ServiceState }) {
  const now = useNow(5000);
  const incident = usePoll(() => api.incident(incidentId), 5000, [incidentId]);
  const tree = usePoll(() => api.cascadeTree(incidentId), 5000, [incidentId]);
  const rca = usePoll(
    () => api.rcaReport(incidentId).catch((e) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e))),
    0,
    [incidentId],
  );
  const [running, setRunning] = useState(false);
  const [rcaError, setRcaError] = useState<string | null>(null);
  const inc = incident.data;

  async function runRca() {
    setRunning(true);
    setRcaError(null);
    try {
      await api.triggerRca(incidentId);
      await rca.refresh();
    } catch (e) {
      setRcaError(errorMessage(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <Panel
        eyebrow="Incident"
        title={<span className="mono">{service.name}</span>}
        actions={inc && <StatusBadge tone={incidentTone(inc)} label={inc.severity ?? inc.status} />}
      >
        {inc ? (
          <>
            <p className="text-[13px]">{inc.trigger_reason}</p>
            <p className="mt-1 text-[12px] text-text-2">
              {inc.status.toLowerCase()} · detected {fmtAgo(parseUtc(inc.detected_at), now)}
              {inc.is_cascading && tree.data ? (
                <>
                  {" "}
                  · cascading from <span className="mono">{tree.data.root.service_name}</span>
                </>
              ) : null}
            </p>
            <Link to={`/incidents/${inc.id}`} className="mt-2 inline-block text-[12px] font-semibold text-text-2 underline-offset-2 hover:text-text hover:underline">
              Open full incident →
            </Link>
          </>
        ) : (
          <div className="text-muted">Loading…</div>
        )}
      </Panel>
      <Panel
        eyebrow="Root-cause analysis"
        title="Ranked candidates"
        actions={
          <button type="button" className="btn" onClick={runRca} disabled={running}>
            {running ? "Analysing…" : rca.data ? "Re-run" : "Run RCA"}
          </button>
        }
      >
        {rcaError && <ErrorNote>{rcaError}</ErrorNote>}
        {rca.data ? <RcaReportView report={rca.data} /> : <Empty>No report yet. RCA collects structured signals first, then ranks them.</Empty>}
      </Panel>
      {tree.data && (tree.data.children.length > 0 || inc?.is_cascading) && (
        <Panel eyebrow="Dependency context" title="Cascade tree">
          <CascadeTreeView tree={tree.data} current={incidentId} />
        </Panel>
      )}
      <Panel eyebrow="SLO" title="Burn rate">
        <BurnPanel service={service.name} minutes={30} />
      </Panel>
    </div>
  );
}
