import { useState } from "react";
import { Link } from "react-router";
import { api, errorMessage, parseUtc } from "../api/client";
import { Empty, ErrorNote, Panel, StatusBadge } from "../components/ui";
import { fmtDateTime, fmtSeconds } from "../lib/format";
import { useNow, usePoll } from "../lib/hooks";
import { incidentTone } from "../lib/status";

const FILTERS = [
  { key: "active", label: "Active" },
  { key: "all", label: "All" },
  { key: "RESOLVED", label: "Resolved" },
] as const;

export default function IncidentsPage() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("active");
  const now = useNow(5000);
  const { data, error } = usePoll(() => api.incidents(), 4000);
  const rows = (data ?? []).filter((i) =>
    filter === "all" ? true : filter === "active" ? i.status !== "RESOLVED" : i.status === "RESOLVED",
  );
  return (
    <div className="space-y-4 pb-6">
      <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Filter incidents">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            role="tab"
            aria-selected={filter === f.key}
            className={`btn ${filter === f.key ? "btn-primary" : ""}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
      {error ? <ErrorNote>{errorMessage(error)}</ErrorNote> : null}
      <Panel eyebrow="Incidents" title={`${rows.length} ${filter === "active" ? "active" : filter === "all" ? "total" : "resolved"}`} bodyClass="p-0">
        {rows.length === 0 ? (
          <div className="p-4">
            <Empty>Nothing here.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[13px]">
              <thead>
                <tr className="text-left text-[12px] text-muted">
                  <th className="px-4 py-2 font-medium">Service</th>
                  <th className="px-2 py-2 font-medium">Severity</th>
                  <th className="px-2 py-2 font-medium">Why</th>
                  <th className="px-2 py-2 font-medium">Detected</th>
                  <th className="px-4 py-2 text-right font-medium">Duration</th>
                </tr>
              </thead>
              <tbody className="num">
                {rows.map((i) => {
                  const d = parseUtc(i.detected_at);
                  const r = parseUtc(i.resolved_at);
                  return (
                    <tr key={i.id} className="border-t border-[var(--ring)] align-top hover:bg-panel-2">
                      <td className="px-4 py-2.5">
                        <Link to={`/incidents/${i.id}`} className="mono font-semibold hover:underline">
                          {i.service_name}
                        </Link>
                        {i.is_cascading && <div className="text-[11px] text-muted">cascading</div>}
                        {i.chaos_experiment_id && <div className="text-[11px] text-muted">chaos drill</div>}
                      </td>
                      <td className="px-2 py-2.5">
                        <StatusBadge tone={incidentTone(i)} label={i.status === "RESOLVED" ? "Resolved" : (i.severity ?? i.status)} />
                      </td>
                      <td className="max-w-[420px] px-2 py-2.5 text-text-2">{i.trigger_reason}</td>
                      <td className="whitespace-nowrap px-2 py-2.5 text-text-2">{fmtDateTime(d)}</td>
                      <td className="mono px-4 py-2.5 text-right">{d ? fmtSeconds(((r ?? now).getTime() - d.getTime()) / 1000) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
