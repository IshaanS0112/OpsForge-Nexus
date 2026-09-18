import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api, errorMessage, parseUtc, type ChaosExperiment, type FaultType, type ServiceState } from "../api/client";
import { Empty, ErrorNote, Panel, StatusBadge, StatusIcon } from "../components/ui";
import { fmtDateTime, fmtSeconds } from "../lib/format";
import { useNow, usePoll } from "../lib/hooks";
import { FAULT_LABEL } from "../lib/status";

const FAULTS: { type: FaultType; what: string }[] = [
  { type: "LATENCY_INJECTION", what: "Every targeted pod adds this many ms to each request: a slow disk, a degraded dependency." },
  { type: "ERROR_BURST", what: "Every targeted pod fails this share of requests with a 500: a bad code path or config." },
  { type: "POD_KILL", what: "Deletes this share of the targeted pods. The ReplicaSet replaces them; each replacement warms up before it is Ready." },
];

export default function ChaosPage() {
  const snap = usePoll(api.snapshot, 3000);
  const exps = usePoll(api.experiments, 2000);
  const running = (exps.data ?? []).filter((e) => e.status === "RUNNING");
  return (
    <div className="space-y-4 pb-6">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[400px_minmax(0,1fr)]">
        <RunDrill services={snap.data?.services ?? []} onStarted={() => exps.refresh()} />
        <div className="space-y-4">
          {running.length === 0 ? (
            <Panel eyebrow="Running" title="No drill running">
              <Empty>
                A drill breaks something for real inside the cluster, then measures the system's own response: how long until
                a detector caught it, how long until a rollback finished, how long until the service recovered.
              </Empty>
            </Panel>
          ) : (
            running.map((e) => <RunningDrill key={e.id} exp={e} onChange={() => exps.refresh()} />)
          )}
        </div>
      </div>
      <PastDrills exps={(exps.data ?? []).filter((e) => e.status === "COMPLETED")} error={exps.error} />
    </div>
  );
}

function RunDrill({ services, onStarted }: { services: ServiceState[]; onStarted: () => void }) {
  const specs = usePoll(api.faultTypes, 0);
  const [service, setService] = useState("payments-svc");
  const [fault, setFault] = useState<FaultType>("LATENCY_INJECTION");
  const [magnitude, setMagnitude] = useState<string>("");
  const [target, setTarget] = useState<"all" | "stable" | "canary">("all");
  const [duration, setDuration] = useState("60");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const spec = specs.data?.[fault];
  const svc = services.find((s) => s.name === service);
  const canaryAvailable = Boolean(svc?.deployment?.status.startsWith("CANARY_"));

  useEffect(() => {
    if (target === "canary" && !canaryAvailable) setTarget("all");
  }, [canaryAvailable, target]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.inject({
        service_name: service,
        fault_type: fault,
        target,
        ...(magnitude ? { magnitude: Number(magnitude) } : {}),
        ...(duration ? { duration_s: Number(duration) } : {}),
      });
      onStarted();
    } catch (x) {
      setErr(errorMessage(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel eyebrow="Chaos drill" title="Break something on purpose">
      <form onSubmit={submit} className="space-y-3">
        <label className="block">
          <span className="mb-1 block text-[12px] font-semibold text-text-2">Service</span>
          <select className="field mono" value={service} onChange={(e) => setService(e.target.value)}>
            {services.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend className="mb-1 text-[12px] font-semibold text-text-2">Fault</legend>
          <div className="space-y-2" role="radiogroup">
            {FAULTS.map((f) => (
              <button
                type="button"
                key={f.type}
                role="radio"
                aria-checked={fault === f.type}
                aria-label={FAULT_LABEL[f.type]}
                aria-describedby={`fault-${f.type}`}
                onClick={() => {
                  setFault(f.type);
                  setMagnitude("");
                }}
                className={`block w-full rounded-lg border px-3 py-2 text-left ${fault === f.type ? "border-[var(--text-2)] bg-panel-2" : "border-grid"}`}
              >
                <span className="block text-[13px] font-semibold">{FAULT_LABEL[f.type]}</span>
                <span id={`fault-${f.type}`} className="block text-[12px] text-text-2">
                  {f.what}
                </span>
              </button>
            ))}
          </div>
        </fieldset>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="mb-1 block text-[12px] font-semibold text-text-2">Magnitude</span>
            <input
              className="field mono"
              type="number"
              step="any"
              min={spec?.min}
              max={spec?.max}
              placeholder={spec ? String(spec.default) : ""}
              value={magnitude}
              onChange={(e) => setMagnitude(e.target.value)}
            />
            <span className="mt-1 block text-[11px] text-muted">{spec ? `${spec.min}–${spec.max} ${spec.unit}` : ""}</span>
          </label>
          <label className="block">
            <span className="mb-1 block text-[12px] font-semibold text-text-2">Duration (s)</span>
            <input className="field mono" type="number" min={10} max={900} value={duration} onChange={(e) => setDuration(e.target.value)} />
            <span className="mt-1 block text-[11px] text-muted">the pod clears the fault itself after this</span>
          </label>
        </div>
        <fieldset>
          <legend className="mb-1 text-[12px] font-semibold text-text-2">Target</legend>
          <div className="grid grid-cols-3 gap-2" role="radiogroup">
            {(["all", "stable", "canary"] as const).map((t) => {
              const disabled = t === "canary" && !canaryAvailable;
              return (
                <button
                  type="button"
                  key={t}
                  role="radio"
                  aria-checked={target === t}
                  disabled={disabled}
                  aria-label={t === "all" ? "All pods" : t === "stable" ? "Stable colour" : "Canary"}
                  onClick={() => setTarget(t)}
                  title={disabled ? "Needs a canary rollout of this service in flight" : undefined}
                  className={`rounded-lg border px-2 py-1.5 text-[12px] font-semibold disabled:opacity-40 ${
                    target === t ? "border-[var(--text-2)] bg-panel-2" : "border-grid"
                  }`}
                >
                  {t === "all" ? "All pods" : t === "stable" ? "Stable colour" : "Canary"}
                </button>
              );
            })}
          </div>
          <p className="mt-1 text-[11px] text-muted">
            Targeting the canary stands in for a bad release: the drill then also expects, and times, a rollback.
          </p>
        </fieldset>
        {err && <ErrorNote>{err}</ErrorNote>}
        <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy}>
          {busy ? "Injecting…" : `Inject ${FAULT_LABEL[fault].toLowerCase()} into ${service}`}
        </button>
      </form>
    </Panel>
  );
}

function RunningDrill({ exp, onChange }: { exp: ChaosExperiment; onChange: () => void }) {
  const now = useNow(1000);
  const [err, setErr] = useState<string | null>(null);
  const injected = parseUtc(exp.injected_at);
  const elapsed = injected ? (now.getTime() - injected.getTime()) / 1000 : 0;
  return (
    <Panel
      eyebrow={`Running · ${FAULT_LABEL[exp.fault_type]}`}
      title={<span className="mono">{exp.service_name}</span>}
      actions={
        <button
          className="btn"
          onClick={async () => {
            try {
              await api.abort(exp.id);
              onChange();
            } catch (e) {
              setErr(errorMessage(e));
            }
          }}
        >
          Abort
        </button>
      }
    >
      {err && <ErrorNote>{err}</ErrorNote>}
      <div className="grid grid-cols-3 gap-3">
        <Latency label="Elapsed" value={elapsed} />
        <Latency label="Detected after" value={exp.detection_latency_seconds} bound={exp.detection_bound_s} />
        <Latency
          label="Rolled back after"
          value={exp.rollback_latency_seconds}
          bound={exp.deployment_id ? exp.rollback_bound_s : null}
          na={!exp.deployment_id}
        />
      </div>
      <Timeline exp={exp} />
    </Panel>
  );
}

/** A measured latency against its pass bound: the meter fills toward the bound. */
function Latency({ label, value, bound, na }: { label: string; value: number | null | undefined; bound?: number | null; na?: boolean }) {
  const within = value != null && bound != null ? value <= bound : null;
  return (
    <div className="min-w-0">
      <div className="text-[12px] text-muted">{label}</div>
      <div className="mono mt-0.5 text-[18px] font-semibold">{na ? "n/a" : value != null ? fmtSeconds(value) : "…"}</div>
      {bound != null && (
        <div className="mt-1 flex items-center gap-1.5">
          <span className="inline-block h-1.5 flex-1 overflow-hidden rounded-full bg-grid" aria-hidden>
            {value != null && (
              <span
                className="block h-full"
                style={{ width: `${Math.min(100, (value / bound) * 100)}%`, background: within ? "var(--ok)" : "var(--critical)" }}
              />
            )}
          </span>
          <span className="mono text-[11px] text-muted">≤{fmtSeconds(bound)}</span>
        </div>
      )}
    </div>
  );
}

function Timeline({ exp }: { exp: ChaosExperiment }) {
  return (
    <ol className="mt-4 space-y-1.5 border-t border-[var(--ring)] pt-3">
      {(exp.timeline ?? []).map((e, i) => (
        <li key={i} className="flex gap-3 text-[12px]">
          <span className="mono w-[56px] shrink-0 text-right text-muted">+{e.t.toFixed(1)}s</span>
          <span className="min-w-0 break-words">{e.event}</span>
        </li>
      ))}
    </ol>
  );
}

function PastDrills({ exps, error }: { exps: ChaosExperiment[]; error: unknown }) {
  const [open, setOpen] = useState<string | null>(null);
  const passed = exps.filter((e) => e.passed).length;
  return (
    <Panel
      eyebrow="Drill reports"
      title={exps.length ? `${passed} of ${exps.length} passed` : "No drills yet"}
      bodyClass="p-0"
    >
      {error ? (
        <div className="p-4">
          <ErrorNote>{errorMessage(error)}</ErrorNote>
        </div>
      ) : exps.length === 0 ? (
        <div className="p-4">
          <Empty>Completed drills land here with their measured latencies and verdict.</Empty>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-[13px]">
            <thead>
              <tr className="text-left text-[12px] text-muted">
                <th className="px-4 py-2 font-medium">Drill</th>
                <th className="px-2 py-2 font-medium">Target</th>
                <th className="px-2 py-2 text-right font-medium">Detected</th>
                <th className="px-2 py-2 text-right font-medium">Rolled back</th>
                <th className="px-2 py-2 text-right font-medium">Recovered</th>
                <th className="px-2 py-2 font-medium">Verdict</th>
                <th className="px-4 py-2 font-medium">When</th>
              </tr>
            </thead>
            <tbody className="num">
              {exps.map((e) => {
                const isOpen = open === e.id;
                return (
                  <Fragment key={e.id}>
                    <tr className="cursor-pointer border-t border-[var(--ring)] hover:bg-panel-2" onClick={() => setOpen(isOpen ? null : e.id)}>
                      <td className="px-4 py-2.5">
                        <button type="button" aria-expanded={isOpen} className="text-left">
                          <span className="font-semibold">
                            {isOpen ? "▾" : "▸"} {FAULT_LABEL[e.fault_type]}
                          </span>{" "}
                          <span className="mono text-text-2">{e.service_name}</span>
                        </button>
                      </td>
                      <td className="px-2 py-2.5 text-text-2">{e.target}</td>
                      <td className="mono px-2 py-2.5 text-right">{fmtSeconds(e.detection_latency_seconds)}</td>
                      <td className="mono px-2 py-2.5 text-right">{e.deployment_id ? fmtSeconds(e.rollback_latency_seconds) : "n/a"}</td>
                      <td className="mono px-2 py-2.5 text-right">{fmtSeconds(e.recovery_latency_seconds)}</td>
                      <td className="px-2 py-2.5">
                        {e.passed == null ? (
                          <span className="text-muted">aborted</span>
                        ) : (
                          <StatusBadge tone={e.passed ? "ok" : "critical"} label={e.passed ? "Pass" : "Fail"} />
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-text-2">{fmtDateTime(parseUtc(e.injected_at))}</td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={7} className="bg-panel-2 px-4 pb-3">
                          {e.notes && (
                            <p className="flex items-start gap-2 pt-3 text-[12px]">
                              <StatusIcon tone={e.passed ? "ok" : e.passed === false ? "critical" : "neutral"} size={13} />
                              {e.notes}
                            </p>
                          )}
                          <Timeline exp={e} />
                          {e.deployment_id && (
                            <Link to="/deployments" className="mt-2 inline-block text-[12px] font-semibold text-text-2 hover:underline">
                              Linked rollout →
                            </Link>
                          )}
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
