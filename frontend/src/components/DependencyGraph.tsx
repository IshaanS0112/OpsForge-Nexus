import { useMemo } from "react";
import type { ServiceState } from "../api/client";
import { fmtMs, fmtPct } from "../lib/format";
import { blastRadius, cascadeEdges, edgeKey, edgePath, layoutGraph, type Edge } from "../lib/graph";
import { STATUS_META } from "../lib/status";
import { StatusIcon } from "./ui";

const NODE = { nodeW: 196, nodeH: 78, colGap: 86, rowGap: 26, pad: 22 };

/**
 * The service graph: the console's centrepiece. Arrows point from a caller to
 * what it depends on; a live cascade animates against them, the way a failure
 * travels. In blast-radius mode everything a selected service could not hurt
 * is dimmed.
 */
export function DependencyGraph({
  services,
  edges,
  selected,
  onSelect,
  blastMode,
  drillServices,
}: {
  services: ServiceState[];
  edges: Edge[];
  selected: string | null;
  onSelect: (name: string | null) => void;
  blastMode: boolean;
  drillServices: Set<string>;
}) {
  const layout = useMemo(() => layoutGraph(services, edges, NODE), [services, edges]);
  const cascading = useMemo(() => cascadeEdges(services, edges), [services, edges]);
  const inBlast = useMemo(() => {
    if (!blastMode || !selected) return null;
    return new Set([selected, ...blastRadius(selected, edges)]);
  }, [blastMode, selected, edges]);

  const dimmed = (name: string) => inBlast != null && !inBlast.has(name);

  return (
    <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        className="block h-auto w-full"
        style={{ minWidth: Math.round(layout.width * 0.72) }}
        role="group"
        aria-label="Service dependency graph"
        onClick={(e) => {
          if (e.target === e.currentTarget) onSelect(null);
        }}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--axis)" />
          </marker>
          <marker id="arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--critical)" />
          </marker>
        </defs>

        {edges.map((e) => {
          const from = layout.nodes.get(e.service);
          const to = layout.nodes.get(e.depends_on);
          if (!from || !to) return null;
          const hot = cascading.has(edgeKey(e));
          const d = edgePath(from, to, NODE.nodeW, NODE.nodeH);
          const faded = inBlast != null && !(inBlast.has(e.service) && inBlast.has(e.depends_on));
          return (
            <g key={edgeKey(e)} opacity={faded ? 0.15 : 1} style={{ transition: "opacity 200ms" }}>
              <path d={d} fill="none" stroke={hot ? "var(--critical)" : "var(--axis)"} strokeWidth={hot ? 2 : 1.5} markerEnd={`url(#${hot ? "arrow-hot" : "arrow"})`} />
              {hot && <path d={d} fill="none" stroke="var(--panel)" strokeWidth={2} className="edge-flow" strokeOpacity={0.7} />}
            </g>
          );
        })}

        {services.map((s) => {
          const p = layout.nodes.get(s.name);
          if (!p) return null;
          const isSel = selected === s.name;
          const color = s.status === "ok" ? "var(--grid)" : `var(--${s.status})`;
          const v = s.values;
          const label = `${s.name}, ${STATUS_META[s.status].label}${v ? `, p95 ${fmtMs(v.latency_p95)}, errors ${fmtPct(v.error_rate, 2)}` : ""}`;
          const weight = s.route.canary_color ? s.route.canary_weight : 0;
          return (
            <g
              key={s.name}
              transform={`translate(${p.x},${p.y})`}
              opacity={dimmed(s.name) ? 0.22 : 1}
              style={{ transition: "opacity 200ms", cursor: "pointer" }}
              tabIndex={0}
              role="button"
              aria-pressed={isSel}
              aria-label={label}
              onClick={() => onSelect(isSel ? null : s.name)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(isSel ? null : s.name);
                }
              }}
            >
              {s.status === "critical" && (
                <rect x={-4} y={-4} width={NODE.nodeW + 8} height={NODE.nodeH + 8} rx={14} fill="none" stroke="var(--critical)" strokeWidth={2} className="pulse-ring" />
              )}
              {isSel && (
                <rect x={-5} y={-5} width={NODE.nodeW + 10} height={NODE.nodeH + 10} rx={15} fill="none" stroke="var(--text-2)" strokeWidth={1.5} />
              )}
              <rect width={NODE.nodeW} height={NODE.nodeH} rx={11} fill="var(--panel-2)" stroke={color} strokeWidth={s.status === "ok" ? 1 : 1.75} />
              <g transform="translate(12,13)">
                <StatusIcon tone={s.status} size={15} />
              </g>
              <text x={34} y={25} className="mono" fontSize="13" fontWeight="600" fill="var(--text)">
                {s.name}
              </text>
              <text x={12} y={46} className="mono" fontSize="11" fill="var(--muted)">
                {s.live_version}
                {s.route.canary_color ? ` · canary ${s.deployment?.version ?? ""}` : ""}
              </text>
              <text x={12} y={64} className="mono num" fontSize="11" fill="var(--text-2)">
                {v ? `p95 ${fmtMs(v.latency_p95)} · err ${fmtPct(v.error_rate, 1)}` : "no traffic yet"}
              </text>
              {(s.incident || drillServices.has(s.name)) && (
                <g transform={`translate(${NODE.nodeW - 12},16)`}>
                  {drillServices.has(s.name) && (
                    <g transform="translate(-14,-6)">
                      <title>Chaos drill running</title>
                      <path d="M5 0L0 7h3.5L2.5 12 8 4.5H4.5z" fill="var(--text-2)" />
                    </g>
                  )}
                  {s.incident && (
                    <g transform={drillServices.has(s.name) ? "translate(-30,-6)" : "translate(-12,-6)"}>
                      <title>{s.incident.is_cascading ? "Incident (cascading)" : "Incident"}</title>
                      <rect width={12} height={12} rx={3} fill="none" stroke="var(--text-2)" strokeWidth={1.2} />
                      <path d="M3.5 3v6M3.5 3h5l-1.2 1.6L8.5 6h-5" fill="none" stroke="var(--text-2)" strokeWidth={1.1} strokeLinejoin="round" />
                    </g>
                  )}
                </g>
              )}
              {weight > 0 && (
                <g transform={`translate(12,${NODE.nodeH - 6})`}>
                  <rect width={NODE.nodeW - 24} height={3} rx={1.5} fill="var(--grid)" />
                  <rect width={((NODE.nodeW - 24) * weight) / 100} height={3} rx={1.5} fill="var(--canary)" />
                </g>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function GraphLegend() {
  const items = (["ok", "warn", "critical", "rollback", "canary"] as const).map((s) => (
    <li key={s} className="inline-flex items-center gap-1.5">
      <StatusIcon tone={s} size={12} />
      {STATUS_META[s].label}
    </li>
  ));
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px] text-text-2">
      {items}
      <li className="inline-flex items-center gap-1.5">
        <svg width="22" height="8" aria-hidden>
          <line x1="0" y1="4" x2="18" y2="4" stroke="var(--axis)" strokeWidth="1.5" />
          <path d="M16 1l5 3-5 3z" fill="var(--axis)" />
        </svg>
        calls
      </li>
      <li className="inline-flex items-center gap-1.5">
        <svg width="22" height="8" aria-hidden>
          <line x1="0" y1="4" x2="18" y2="4" stroke="var(--critical)" strokeWidth="2" />
          <path d="M16 1l5 3-5 3z" fill="var(--critical)" />
        </svg>
        cascade path
      </li>
    </ul>
  );
}
