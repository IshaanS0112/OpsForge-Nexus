import { Link } from "react-router";
import type { GateVerdict, ServiceState, Snapshot } from "../api/client";
import { parseUtc } from "../api/client";
import { fmtAgo, fmtBurn, fmtMs, fmtPct } from "../lib/format";
import { useNow } from "../lib/hooks";
import { DEPLOY_LABEL, STATUS_META } from "../lib/status";
import { HealthRing } from "./charts";
import { StatusIcon } from "./ui";

// ------------------------------------------------------------ status strip --
export function StatusStrip({ snap }: { snap: Snapshot }) {
  const now = useNow(1000);
  const { summary: s, cluster } = snap;
  const worst = s.worst_burn;
  const tick = parseUtc(snap.loop.last_tick_at);
  const stale = tick ? (now.getTime() - tick.getTime()) / 1000 > snap.loop.tick_s * 4 : true;
  const rollouts = snap.services.filter((x) => x.deployment);
  return (
    <div className="panel grid grid-cols-2 divide-[var(--ring)] sm:grid-cols-3 xl:flex xl:divide-x">
      <div className="flex items-center gap-3 px-4 py-3 xl:min-w-[170px]">
        <HealthRing statuses={snap.services.map((x) => x.status)} />
        <div>
          <div className="text-[20px] font-bold leading-none">
            {s.healthy}
            <span className="text-[14px] font-semibold text-muted"> / {s.services}</span>
          </div>
          <div className="mt-1 text-[12px] text-text-2">services healthy</div>
        </div>
      </div>
      <Link to="/incidents" className="flex items-center gap-3 px-4 py-3 hover:bg-panel-2 xl:min-w-[150px]">
        <StatusIcon tone={s.open_incidents ? "critical" : "ok"} size={18} />
        <div>
          <div className="text-[20px] font-bold leading-none">{s.open_incidents}</div>
          <div className="mt-1 text-[12px] text-text-2">open incident{s.open_incidents === 1 ? "" : "s"}</div>
        </div>
      </Link>
      <div className="flex min-w-0 items-center gap-3 px-4 py-3 xl:min-w-[210px]">
        <StatusIcon tone={worst ? (worst.severity === "FAST_BURN" ? "critical" : "warn") : "ok"} size={18} />
        <div className="min-w-0">
          {worst ? (
            <>
              <div className="mono text-[18px] font-bold leading-none">{fmtBurn(worst.burn)}</div>
              <div className="mt-1 truncate text-[12px] text-text-2">
                {worst.severity.replace("_", " ").toLowerCase()} · <span className="mono">{worst.service}</span>
              </div>
            </>
          ) : (
            <>
              <div className="mono text-[18px] font-bold leading-none">{fmtBurn(s.max_burn_1h?.burn ?? 0)}</div>
              <div className="mt-1 truncate text-[12px] text-text-2">worst 1h burn · no alert firing</div>
            </>
          )}
        </div>
      </div>
      <Link to="/deployments" className="flex min-w-0 items-center gap-3 px-4 py-3 hover:bg-panel-2 xl:min-w-[190px]">
        <StatusIcon tone={rollouts.length ? "canary" : "neutral"} size={18} />
        <div className="min-w-0">
          <div className="text-[20px] font-bold leading-none">{rollouts.length}</div>
          <div className="mt-1 truncate text-[12px] text-text-2">
            {rollouts.length ? (
              <>
                rollout · <span className="mono">{rollouts[0].name}</span> {DEPLOY_LABEL[rollouts[0].deployment!.status] ?? ""}
              </>
            ) : (
              "rollouts in flight"
            )}
          </div>
        </div>
      </Link>
      <Link to="/chaos" className="flex items-center gap-3 px-4 py-3 hover:bg-panel-2 xl:min-w-[150px]">
        <StatusIcon tone="neutral" size={18} />
        <div>
          <div className="text-[20px] font-bold leading-none">{s.running_drills}</div>
          <div className="mt-1 text-[12px] text-text-2">chaos drill{s.running_drills === 1 ? "" : "s"} running</div>
        </div>
      </Link>
      <div className="flex min-w-0 items-center gap-3 px-4 py-3 xl:ml-auto xl:min-w-[200px]">
        <div className="min-w-0 xl:text-right">
          <div className="mono text-[13px] font-semibold">
            {cluster.mode === "kind" ? "kind · real cluster" : "SIMULATED cluster"}
          </div>
          <div className="mt-0.5 truncate text-[12px] text-text-2">
            {cluster.error ? (
              <span title={cluster.error}>cluster unreachable — retrying</span>
            ) : (
              <>
                ns <span className="mono">{cluster.namespace}</span> · tick {fmtAgo(tick, now)}
                {stale && tick ? " (stale)" : ""}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- service rail --
export function ServiceRail({
  services,
  selected,
  onSelect,
}: {
  services: ServiceState[];
  selected: string | null;
  onSelect: (name: string | null) => void;
}) {
  return (
    <nav aria-label="Services" className="panel p-2">
      <div className="eyebrow px-2 pb-2 pt-1">Services</div>
      <ul className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
        {services.map((s) => {
          const active = selected === s.name;
          return (
            <li key={s.name} className="shrink-0">
              <button
                type="button"
                onClick={() => onSelect(active ? null : s.name)}
                aria-pressed={active}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors ${
                  active ? "bg-panel-2 ring-1 ring-[var(--axis)]" : "hover:bg-panel-2"
                }`}
                title={STATUS_META[s.status].hint}
              >
                <StatusIcon tone={s.status} size={14} />
                <span className="min-w-0 flex-1">
                  <span className="mono block truncate text-[13px] font-semibold">{s.name}</span>
                  <span className="block truncate text-[11px] text-muted">
                    {STATUS_META[s.status].label}
                    {s.values ? ` · p95 ${fmtMs(s.values.latency_p95)}` : ""}
                    {s.values?.error_rate ? ` · ${fmtPct(s.values.error_rate, 1)} err` : ""}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

// ---------------------------------------------------------------- canary rail --
const STAGES = [5, 25, 50, 100];

function stageIndex(status: string): number {
  const m = status.match(/^CANARY_(\d+)$/);
  if (m) return STAGES.indexOf(Number(m[1]));
  if (status === "TRAFFIC_SWITCHING" || status === "LIVE") return STAGES.length;
  return -1;
}

export function GateLine({ gate }: { gate: GateVerdict | null | undefined }) {
  if (!gate) return <span className="text-muted">waiting for the first gate evaluation</span>;
  const pct = gate.window_s > 0 ? Math.min(100, (gate.elapsed_s / gate.window_s) * 100) : 100;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="mono">
        canary {fmtBurn(gate.canary_burn)} <span className="text-muted">vs {fmtBurn(gate.threshold)}</span>
      </span>
      <span className="inline-flex items-center gap-1.5 text-text-2" title="Wald SPRT: evidence the canary burns at the page rate rather than at budget. Crossing the boundary fails the stage.">
        <span className="text-muted">evidence</span>
        <span className="inline-block h-1.5 w-12 overflow-hidden rounded-full bg-grid">
          <span
            className="block h-full"
            style={{
              width: `${Math.max(0, Math.min(100, (gate.sprt_llr / gate.sprt_fail_at) * 100))}%`,
              background: gate.sprt_llr >= gate.sprt_fail_at ? "var(--critical)" : "var(--data-2)",
            }}
          />
        </span>
        <span className="mono">
          {Math.max(0, gate.sprt_llr).toFixed(2)}/{gate.sprt_fail_at.toFixed(2)}
        </span>
      </span>
      <span className="mono text-text-2">
        {gate.canary_bad}/{gate.canary_requests} bad · min {gate.min_requests}
      </span>
      <span className="inline-flex items-center gap-1.5 text-text-2">
        <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-grid">
          <span className="block h-full bg-[var(--canary)]" style={{ width: `${pct}%` }} />
        </span>
        <span className="mono">
          {Math.round(gate.elapsed_s)}/{Math.round(gate.window_s)}s
        </span>
      </span>
    </span>
  );
}

/**
 * The rollout rail: 5 → 25 → 50 → 100% gates for a canary, each lighting up as
 * it clears. Shown only while a rollout is in flight.
 */
export function CanaryRail({ service }: { service: ServiceState }) {
  const dep = service.deployment!;
  const idx = stageIndex(dep.status);
  const isCanary = dep.strategy === "CANARY";
  const failed = dep.status === "ROLLING_BACK";
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-center">
      <div className="min-w-[180px]">
        <div className="eyebrow">{isCanary ? "Canary rollout" : "Blue-green rollout"}</div>
        <div className="mono mt-0.5 text-[14px] font-semibold">
          {service.name} <span className="text-muted">{service.live_version} →</span> {dep.version}
        </div>
        <div className="mt-0.5 text-[12px] text-text-2">{DEPLOY_LABEL[dep.status] ?? dep.status}</div>
      </div>
      {isCanary ? (
        <ol className="flex flex-1 items-center" aria-label="Canary stages">
          {STAGES.map((w, i) => {
            const cleared = idx > i;
            const current = idx === i;
            const tone = failed && current ? "critical" : cleared ? "ok" : current ? "canary" : "neutral";
            return (
              <li key={w} className="flex flex-1 items-center last:flex-none">
                <span className="flex flex-col items-center gap-1">
                  <span
                    className={`flex h-7 w-7 items-center justify-center rounded-full border-2 ${current && !failed ? "breathe" : ""}`}
                    style={{
                      borderColor: tone === "neutral" ? "var(--grid)" : `var(--${tone})`,
                      background: cleared ? "var(--ok)" : "transparent",
                    }}
                    aria-label={`${w}% ${cleared ? "cleared" : current ? "in progress" : "pending"}`}
                  >
                    {cleared ? (
                      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
                        <path d="M2.5 6.2l2.3 2.3 4.7-5" fill="none" stroke="var(--panel)" strokeWidth="1.8" strokeLinecap="round" />
                      </svg>
                    ) : null}
                  </span>
                  <span className="mono text-[12px] font-semibold">{w}%</span>
                </span>
                {i < STAGES.length - 1 && (
                  <span className="mx-2 mb-5 h-0.5 flex-1 rounded" style={{ background: cleared ? "var(--ok)" : "var(--grid)" }} />
                )}
              </li>
            );
          })}
        </ol>
      ) : (
        <div className="flex-1 text-[13px] text-text-2">{dep.status_reason}</div>
      )}
      <div className="text-[12px] md:max-w-[440px]">
        {dep.status.startsWith("CANARY_") ? <GateLine gate={dep.gate} /> : <span className="text-text-2">{dep.status_reason}</span>}
      </div>
    </div>
  );
}
