import { Fragment, useMemo, useState, type FormEvent } from "react";
import { api, errorMessage, parseUtc, type Deployment, type K8sStatus, type RolloutStatus, type ServiceState } from "../api/client";
import { CanaryRail, GateLine } from "../components/console";
import { ColorChip, Empty, ErrorNote, Panel, StatusBadge, StatusIcon } from "../components/ui";
import { fmtClock, fmtDateTime, fmtSeconds } from "../lib/format";
import { usePoll } from "../lib/hooks";
import { DEPLOY_LABEL, deploymentTone } from "../lib/status";

const VERSION_RE = /^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$/;
const TERMINAL = new Set(["LIVE", "ROLLED_BACK"]);

function bumpMinor(v: string): string {
  const m = v.match(/^v?(\d+)\.(\d+)\.(\d+)$/);
  return m ? `v${m[1]}.${Number(m[2]) + 1}.0` : `${v}-next`;
}

export default function DeploymentsPage() {
  const snap = usePoll(api.snapshot, 2000);
  const deps = usePoll(() => api.deployments(), 3000);
  const services = snap.data?.services ?? [];
  const active = (deps.data ?? []).filter((d) => !TERMINAL.has(d.status));

  return (
    <div className="space-y-4 pb-6">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
        <NewRollout services={services} onCreated={() => deps.refresh()} />
        <div className="space-y-4">
          {active.length === 0 ? (
            <Panel eyebrow="In flight" title="No rollout in progress">
              <Empty>
                Start one on the left. A canary sends 5% → 25% → 50% → 100% of edge traffic to the new colour, each stage gated on
                its own SLO burn rate; a blue-green rollout runs the health gate, then switches the live Service selector.
              </Empty>
            </Panel>
          ) : (
            active.map((d) => (
              <ActiveRollout
                key={d.id}
                dep={d}
                service={services.find((s) => s.name === d.service_name)}
                onChange={() => deps.refresh()}
              />
            ))
          )}
        </div>
      </div>
      <History deps={deps.data ?? []} error={deps.error} />
    </div>
  );
}

function NewRollout({ services, onCreated }: { services: ServiceState[]; onCreated: () => void }) {
  const [service, setService] = useState("payments-svc");
  const current = services.find((s) => s.name === service);
  const [version, setVersion] = useState("");
  const [strategy, setStrategy] = useState<"CANARY" | "BLUE_GREEN">("CANARY");
  const [simulateFailure, setSimulateFailure] = useState(false);
  const [autoPromote, setAutoPromote] = useState(true);
  const [image, setImage] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const suggested = current ? bumpMinor(current.live_version) : "v1.1.0";
  const effectiveVersion = version || suggested;
  const valid = VERSION_RE.test(effectiveVersion);
  const inFlight = Boolean(current?.deployment);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const d = await api.createDeployment({
        service_name: service,
        version: effectiveVersion,
        strategy,
        simulate_failure: simulateFailure,
        auto_promote: strategy === "CANARY" ? autoPromote : true,
        ...(image.trim() ? { image: image.trim() } : {}),
      });
      setMessage({ ok: true, text: `Rolling ${d.version} out to ${d.k8s_deployment_name}.` });
      setVersion("");
      onCreated();
    } catch (err) {
      setMessage({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel eyebrow="Deploy" title="New rollout">
      <form onSubmit={submit} className="space-y-3">
        <label className="block">
          <span className="mb-1 block text-[12px] font-semibold text-text-2">Service</span>
          <select className="field mono" value={service} onChange={(e) => setService(e.target.value)}>
            {services.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} ({s.live_version})
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-[12px] font-semibold text-text-2">Version</span>
          <input
            className="field mono"
            value={version}
            placeholder={suggested}
            onChange={(e) => setVersion(e.target.value)}
            aria-invalid={!valid}
            spellCheck={false}
          />
          {!valid && <span className="mt-1 block text-[12px] text-text-2">Letters, digits, - _ . only (it becomes a Kubernetes label).</span>}
        </label>
        <fieldset>
          <legend className="mb-1 text-[12px] font-semibold text-text-2">Strategy</legend>
          <div className="grid grid-cols-2 gap-2" role="radiogroup">
            {(["CANARY", "BLUE_GREEN"] as const).map((s) => (
              <button
                type="button"
                key={s}
                role="radio"
                aria-checked={strategy === s}
                onClick={() => setStrategy(s)}
                className={`rounded-lg border px-3 py-2 text-left text-[12px] ${strategy === s ? "border-[var(--text-2)] bg-panel-2" : "border-grid"}`}
              >
                <span className="block font-semibold text-text">{s === "CANARY" ? "Canary" : "Blue-green"}</span>
                <span className="text-text-2">{s === "CANARY" ? "5 → 25 → 50 → 100%, burn-rate gated" : "health gate, then one switch"}</span>
              </button>
            ))}
          </div>
        </fieldset>
        <div className="space-y-2 text-[13px]">
          {strategy === "CANARY" && (
            <label className="flex items-start gap-2">
              <input type="checkbox" className="mt-0.5" checked={autoPromote} onChange={(e) => setAutoPromote(e.target.checked)} />
              <span>
                Auto-promote passing stages
                <span className="block text-[12px] text-text-2">Off: each passing stage waits for Promote (the gate is re-checked).</span>
              </span>
            </label>
          )}
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-0.5" checked={simulateFailure} onChange={(e) => setSimulateFailure(e.target.checked)} />
            <span>
              Ship a bad config
              <span className="block text-[12px] text-text-2">Sets FAULT_ERROR_RATE=0.15 on the new pods. Detection never sees this flag, only the traffic.</span>
            </span>
          </label>
        </div>
        <details className="text-[12px]">
          <summary className="cursor-pointer font-semibold text-text-2">Image override</summary>
          <input
            className="field mono mt-2"
            value={image}
            placeholder="python:3.12-alpine (default)"
            onChange={(e) => setImage(e.target.value)}
            spellCheck={false}
          />
          <span className="mt-1 block text-text-2">An image that does not exist fails the rollout on a real ErrImagePull.</span>
        </details>
        {message && (message.ok ? <p className="text-[12px] text-text-2">{message.text}</p> : <ErrorNote>{message.text}</ErrorNote>)}
        <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy || !valid || inFlight}>
          {inFlight ? `${service} already has a rollout in flight` : busy ? "Starting…" : `Deploy ${effectiveVersion}`}
        </button>
      </form>
    </Panel>
  );
}

function ActiveRollout({ dep, service, onChange }: { dep: Deployment; service?: ServiceState; onChange: () => void }) {
  const k8s = usePoll(() => api.k8sStatus(dep.id), 2000, [dep.id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const isCanaryStage = dep.status.startsWith("CANARY_");

  async function act(name: string, fn: () => Promise<unknown>) {
    setBusy(name);
    setErr(null);
    try {
      await fn();
      onChange();
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel
      eyebrow={`${dep.strategy === "CANARY" ? "Canary" : "Blue-green"} rollout · ${dep.k8s_namespace ?? ""}`}
      title={
        <span className="mono">
          {dep.service_name} → {dep.version}
        </span>
      }
      actions={
        <>
          {isCanaryStage && (
            <button className="btn" disabled={busy != null} onClick={() => act("promote", () => api.promote(dep.id))}>
              {busy === "promote" ? "Checking gate…" : "Promote"}
            </button>
          )}
          <button className="btn btn-danger" disabled={busy != null || dep.status === "IDLE"} onClick={() => act("rollback", () => api.rollback(dep.id))}>
            <StatusIcon tone="rollback" size={13} />
            {busy === "rollback" ? "Rolling back…" : "Roll back"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        {service?.deployment ? <CanaryRail service={service} /> : <p className="text-[13px] text-text-2">{DEPLOY_LABEL[dep.status]}</p>}
        {err && <ErrorNote>{err}</ErrorNote>}
        {isCanaryStage && k8s.data?.gate && (
          <div className="rounded-lg bg-panel-2 px-3 py-2 text-[12px]">
            <div className="mb-1 font-semibold">Stage gate</div>
            <GateLine gate={k8s.data.gate} />
            <div className="mt-1 text-text-2">{k8s.data.gate.reason}</div>
          </div>
        )}
        {k8s.data ? <K8sView status={k8s.data} dep={dep} /> : k8s.error ? <ErrorNote>{errorMessage(k8s.error)}</ErrorNote> : null}
      </div>
    </Panel>
  );
}

function isRollout(x: unknown): x is RolloutStatus {
  return Boolean(x && typeof x === "object" && "pods" in x);
}

function K8sView({ status, dep }: { status: K8sStatus; dep: Deployment }) {
  const split = status.routing.canary_color
    ? `${100 - status.routing.canary_weight}% ${status.routing.live_color} / ${status.routing.canary_weight}% ${status.routing.canary_color}`
    : `100% via live Service`;
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-text-2">
        <span>
          {status.cluster_mode === "kind" ? "Read from the cluster" : "Simulated cluster"} · live Service selector →{" "}
          <ColorChip color={status.live_color} />
        </span>
        <span>
          edge split: <span className="mono">{split}</span>
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {(
          [
            ["Stable", status.stable, dep.stable_color],
            ["Candidate", status.candidate, dep.target_color],
          ] as const
        ).map(([label, r, color]) => (
          <div key={label} className="min-w-0 rounded-lg bg-panel-2 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[12px] font-semibold">
                {label} <ColorChip color={color} />
              </span>
              {isRollout(r) && <span className="mono text-[12px] text-text-2">{r.version}</span>}
            </div>
            {isRollout(r) ? (
              <>
                <div className="mt-1 text-[12px] text-text-2">
                  <span className="mono">
                    {r.available}/{r.desired}
                  </span>{" "}
                  available · {r.message}
                </div>
                <ul className="mt-2 space-y-1">
                  {r.pods.length === 0 && <li className="text-[12px] text-muted">no pods (scaled to zero)</li>}
                  {r.pods.map((p) => (
                    <li key={p.name} className="flex items-center gap-2 text-[12px]">
                      <StatusIcon tone={p.waiting_reason ? "critical" : p.ready ? "ok" : "neutral"} size={12} />
                      <span className="mono min-w-0 flex-1 truncate" title={p.name}>
                        {p.name}
                      </span>
                      <span className="mono shrink-0 text-text-2">{p.waiting_reason ?? (p.ready ? "Ready" : p.phase)}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <div className="mt-1 text-[12px] text-text-2">{r && "error" in r ? r.error : "—"}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function History({ deps, error }: { deps: Deployment[]; error: unknown }) {
  const [open, setOpen] = useState<string | null>(null);
  const rows = useMemo(() => deps.slice(0, 30), [deps]);
  return (
    <Panel eyebrow="History" title="Rollouts" bodyClass="p-0">
      {error ? (
        <div className="p-4">
          <ErrorNote>{errorMessage(error)}</ErrorNote>
        </div>
      ) : rows.length === 0 ? (
        <div className="p-4">
          <Empty>No rollouts yet.</Empty>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-[13px]">
            <thead>
              <tr className="text-left text-[12px] text-muted">
                <th className="px-4 py-2 font-medium">Service</th>
                <th className="px-2 py-2 font-medium">Version</th>
                <th className="px-2 py-2 font-medium">Strategy</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Started</th>
                <th className="px-4 py-2 text-right font-medium">Took</th>
              </tr>
            </thead>
            <tbody className="num">
              {rows.map((d) => {
                const start = parseUtc(d.deployed_at);
                const end = parseUtc(d.finished_at);
                const isOpen = open === d.id;
                return (
                  <Fragment key={d.id}>
                    <tr className="cursor-pointer border-t border-[var(--ring)] hover:bg-panel-2" onClick={() => setOpen(isOpen ? null : d.id)}>
                      <td className="mono px-4 py-2.5 font-semibold">
                        <button type="button" aria-expanded={isOpen} className="text-left">
                          {isOpen ? "▾" : "▸"} {d.service_name}
                        </button>
                      </td>
                      <td className="mono px-2 py-2.5">{d.version}</td>
                      <td className="px-2 py-2.5 text-text-2">{d.strategy === "CANARY" ? "Canary" : "Blue-green"}</td>
                      <td className="px-2 py-2.5">
                        <StatusBadge tone={deploymentTone(d.status)} label={DEPLOY_LABEL[d.status] ?? d.status} />
                      </td>
                      <td className="whitespace-nowrap px-2 py-2.5 text-text-2">{fmtDateTime(start)}</td>
                      <td className="mono px-4 py-2.5 text-right">{start && end ? fmtSeconds((end.getTime() - start.getTime()) / 1000) : "—"}</td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={6} className="bg-panel-2 px-4 py-3">
                          <ol className="space-y-1.5">
                            {(d.stage_history ?? []).map((h, i) => (
                              <li key={i} className="flex gap-3 text-[12px]">
                                <span className="mono w-[76px] shrink-0 text-muted">{fmtClock(parseUtc(h.at))}</span>
                                <span className="w-[130px] shrink-0 font-semibold">{DEPLOY_LABEL[h.stage] ?? h.stage}</span>
                                <span className="min-w-0 break-words text-text-2">{h.reason}</span>
                              </li>
                            ))}
                          </ol>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
