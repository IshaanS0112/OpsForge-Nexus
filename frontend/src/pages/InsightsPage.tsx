import { useState } from "react";
import { api, errorMessage, parseUtc } from "../api/client";
import { BarList, Kpi, SloTable, StrategyBreakdown, formatDuration, formatMoney } from "../components/insights";
import { ErrorNote, Panel } from "../components/ui";
import { dash, fmtDateTime, fmtPct } from "../lib/format";
import { usePoll } from "../lib/hooks";

const WINDOWS = [
  { days: 1, label: "24h" },
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
] as const;

export default function InsightsPage() {
  const [days, setDays] = useState<number>(7);
  const { data, error, loading } = usePoll(() => api.analytics(days), 30000, [days]);

  if (!data) {
    return error ? (
      <ErrorNote>Cannot reach the analytics API: {errorMessage(error)}</ErrorNote>
    ) : (
      <div className="py-24 text-center text-muted">{loading ? "Computing…" : "No data yet."}</div>
    );
  }

  const { delivery, reliability, response, cost } = data;
  const failureRate = delivery.change_failure_rate;
  const headline =
    failureRate == null
      ? "No rollouts finished in this window"
      : `${fmtPct(failureRate, 0)} of rollouts were taken back, and ${reliability.services_meeting_slo} of ${reliability.services.length} services held their objective`;

  return (
    <div className="space-y-4 pb-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <span className="eyebrow">Reliability analytics</span>
          <h1 className="mt-1 max-w-3xl text-[20px] font-bold leading-snug tracking-tight">{headline}</h1>
          <p className="mt-1 text-[12px] text-muted">
            Derived from stored rollouts, incidents, request counts and drills · computed {fmtDateTime(parseUtc(data.generated_at))}
          </p>
        </div>
        <div className="flex gap-1" role="group" aria-label="Window">
          {WINDOWS.map((w) => (
            <button
              key={w.days}
              type="button"
              aria-pressed={days === w.days}
              className={`btn ${days === w.days ? "btn-primary" : ""}`}
              onClick={() => setDays(w.days)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {error ? <ErrorNote>Last refresh failed: {errorMessage(error)}. Showing the previous figures.</ErrorNote> : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi
          label="Change failure rate"
          value={failureRate == null ? dash : fmtPct(failureRate, 0)}
          sub={`${delivery.releases} released · ${delivery.rollbacks} rolled back`}
          tone={failureRate == null ? undefined : failureRate > 0.15 ? "critical" : "ok"}
        />
        <Kpi
          label="Release frequency"
          value={delivery.per_day == null ? dash : `${delivery.per_day}/day`}
          sub={`${formatDuration(delivery.duration_p50_s)} median, ${formatDuration(delivery.duration_p95_s)} p95`}
        />
        <Kpi
          label="Fleet availability"
          value={fmtPct(reliability.fleet_availability, 3)}
          sub={reliability.worst_service ? `worst: ${reliability.worst_service}` : "no traffic measured"}
          tone={reliability.fleet_availability == null ? undefined : reliability.services_meeting_slo === reliability.services.length ? "ok" : "warn"}
        />
        <Kpi
          label="Incident cost"
          value={formatMoney(cost.total_value)}
          sub={`${cost.priced_incidents} incident${cost.priced_incidents === 1 ? "" : "s"} priced`}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <Panel eyebrow="Reliability" title="Availability against each service's own objective">
          <SloTable services={reliability.services} />
        </Panel>

        <Panel eyebrow="Delivery" title="How rollouts ended, by strategy">
          <StrategyBreakdown byStrategy={delivery.by_strategy} />
        </Panel>

        <Panel
          eyebrow="Response"
          title={
            response.incidents === 0
              ? "No incidents in this window"
              : `${response.root_incidents} root causes behind ${response.incidents} incidents`
          }
        >
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
            <Kpi flat label="Time to restore" value={formatDuration(response.mttr_s)} sub={`${response.resolved} resolved`} />
            <Kpi flat label="Longest (p95)" value={formatDuration(response.duration_p95_s)} />
            <Kpi flat label="Time to detect" value={formatDuration(response.mttd_s)} sub="measured by drills" />
            <Kpi
              flat
              label="Drills passed"
              value={response.drills === 0 ? dash : `${response.drills_passed}/${response.drills}`}
              sub={response.recovery_p95_s == null ? undefined : `${formatDuration(response.recovery_p95_s)} to recover`}
            />
          </div>
          <div className="mt-4 border-t border-[var(--ring)] pt-3">
            <div className="eyebrow mb-2">What caught them first</div>
            <BarList
              rows={Object.entries(response.detection_sources).map(([key, value]) => ({ key, value }))}
              format={(v) => `${v}`}
              empty="Nothing fired in this window."
            />
          </div>
        </Panel>

        <Panel eyebrow="Cost" title={cost.worst_service ? `${cost.worst_service} carried the most impact` : "Nothing priced in this window"}>
          <BarList
            rows={cost.by_service.map((s) => ({
              key: s.service,
              value: s.value,
              note: `${s.incidents} incident${s.incidents === 1 ? "" : "s"}`,
            }))}
            format={(v) => formatMoney(v)}
            empty="No incident was priced in this window."
          />
          <p className="mt-3 text-[12px] text-muted">
            Requests that failed beyond the baseline, priced per request. Revenue per request is a configurable
            parameter, and every input is stored with the figure.
          </p>
        </Panel>
      </div>
    </div>
  );
}
