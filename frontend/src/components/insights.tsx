import type { ReactNode } from "react";
import type { AnalyticsSummary } from "../api/client";
import { dash, fmtCompact, fmtMoney, fmtPct, fmtSeconds } from "../lib/format";
import type { Tone } from "../lib/status";
import { StatusIcon } from "./ui";

/** Headline figure. `tone` is only set when the number itself carries a verdict. */
export function Kpi({
  label,
  value,
  sub,
  tone,
  flat = false,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  flat?: boolean;
}) {
  return (
    <div className={`flex min-w-0 flex-col gap-1 rounded-lg px-4 py-3 ${flat ? "bg-panel-2" : "panel"}`}>
      <div className="flex items-start gap-1.5">
        {tone && <span className="shrink-0"><StatusIcon tone={tone} size={13} /></span>}
        <span className="eyebrow">{label}</span>
      </div>
      <div className="mono text-[24px] font-bold leading-none tracking-tight">{value}</div>
      {sub && <div className="truncate text-[12px] text-text-2">{sub}</div>}
    </div>
  );
}

/** Horizontal bars: a share is easier to compare by length than by reading numbers. */
export function BarList({
  rows,
  format,
  empty,
}: {
  rows: { key: string; value: number; note?: string }[];
  format: (v: number) => string;
  empty: string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0);
  if (rows.length === 0 || max <= 0) {
    return <div className="rounded-lg border border-dashed border-grid px-4 py-6 text-center text-[13px] text-muted">{empty}</div>;
  }
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.key}>
          <div className="flex items-baseline justify-between gap-3 text-[12px]">
            <span className="mono min-w-0 truncate font-semibold">{r.key}</span>
            <span className="mono shrink-0 text-text-2">
              {format(r.value)}
              {r.note ? ` · ${r.note}` : ""}
            </span>
          </div>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-panel-2">
            <div
              className="h-full rounded-full"
              style={{ width: `${Math.max(2, (r.value / max) * 100)}%`, background: "var(--data)" }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Availability against each service's own objective, worst first. */
export function SloTable({ services }: { services: AnalyticsSummary["reliability"]["services"] }) {
  if (services.length === 0) {
    return <div className="rounded-lg border border-dashed border-grid px-4 py-6 text-center text-[13px] text-muted">No traffic measured in this window.</div>;
  }
  const ordered = [...services].sort((a, b) => (a.availability ?? 1) - (b.availability ?? 1));
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[460px] text-[13px]">
        <thead>
          <tr className="text-left text-[12px] text-muted">
            <th className="py-1.5 font-medium">Service</th>
            <th className="py-1.5 text-right font-medium">Requests</th>
            <th className="py-1.5 text-right font-medium">Availability</th>
            <th className="py-1.5 text-right font-medium">Objective</th>
            <th className="py-1.5 text-right font-medium">Budget used</th>
          </tr>
        </thead>
        <tbody className="num">
          {ordered.map((s) => (
            <tr key={s.service} className="border-t border-[var(--ring)]">
              <td className="py-2">
                <span className="flex items-center gap-1.5">
                  <StatusIcon tone={s.meets_slo === false ? "critical" : "ok"} size={12} />
                  <span className="mono font-semibold">{s.service}</span>
                </span>
              </td>
              <td className="mono py-2 text-right text-text-2">{fmtCompact(s.requests)}</td>
              <td className="mono py-2 text-right font-semibold">{fmtPct(s.availability, 3)}</td>
              <td className="mono py-2 text-right text-text-2">{fmtPct(s.slo_target, 1)}</td>
              <td className="mono py-2 text-right text-text-2">
                {s.budget_consumed == null ? dash : s.budget_consumed > 1 ? "spent" : fmtPct(s.budget_consumed, 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Where releases ended up, per strategy. */
export function StrategyBreakdown({ byStrategy }: { byStrategy: AnalyticsSummary["delivery"]["by_strategy"] }) {
  const rows = Object.entries(byStrategy);
  if (rows.length === 0) {
    return <div className="rounded-lg border border-dashed border-grid px-4 py-6 text-center text-[13px] text-muted">No rollouts finished in this window.</div>;
  }
  return (
    <ul className="space-y-3">
      {rows.map(([strategy, counts]) => {
        const total = counts.released + counts.rolled_back;
        const released = total ? (counts.released / total) * 100 : 0;
        return (
          <li key={strategy}>
            <div className="flex items-baseline justify-between text-[12px]">
              <span className="mono font-semibold">{strategy === "BLUE_GREEN" ? "Blue-green" : "Canary"}</span>
              <span className="mono text-text-2">
                {counts.released} released · {counts.rolled_back} rolled back
              </span>
            </div>
            <div className="mt-1 flex h-2 w-full overflow-hidden rounded-full bg-panel-2">
              <div style={{ width: `${released}%`, background: "var(--ok)" }} />
              <div style={{ width: `${100 - released}%`, background: "var(--rollback)" }} />
            </div>
          </li>
        );
      })}
      <li className="flex gap-4 pt-1 text-[12px] text-muted">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: "var(--ok)" }} /> released
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: "var(--rollback)" }} /> rolled back
        </span>
      </li>
    </ul>
  );
}

export function formatDuration(seconds: number | null): string {
  return seconds == null ? dash : fmtSeconds(seconds);
}

export function formatMoney(value: number | null): string {
  return value == null ? dash : fmtMoney(value);
}
