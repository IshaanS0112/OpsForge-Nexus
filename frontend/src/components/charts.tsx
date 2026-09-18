import { useId, useMemo, useState, type KeyboardEvent, type PointerEvent } from "react";
import { fmtBurn, fmtClock } from "../lib/format";
import { useWidth } from "../lib/hooks";
import type { Tone } from "../lib/status";

export interface Point {
  t: Date;
  v: number;
}

const toneVar = (t: Tone) => (t === "neutral" ? "var(--data)" : `var(--${t})`);

function nearestIndex(xs: number[], x: number): number {
  let best = 0;
  for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
  return best;
}

function Tooltip({ x, width, children }: { x: number; width: number; children: React.ReactNode }) {
  const left = Math.min(Math.max(0, x - 70), Math.max(0, width - 140));
  return (
    <div
      role="status"
      className="pointer-events-none absolute top-0 z-10 w-[140px] rounded-md border border-grid bg-panel px-2 py-1.5 text-[12px] shadow-lg"
      style={{ left }}
    >
      {children}
    </div>
  );
}

/**
 * Single-series sparkline. No legend: the label beside it names the series.
 * Neutral data ink; the end dot may carry the service's status.
 */
export function Sparkline({
  points,
  format,
  label,
  height = 44,
  tone = "neutral",
  threshold,
}: {
  points: Point[];
  format: (v: number) => string;
  label: string;
  height?: number;
  tone?: Tone;
  threshold?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = 4;
  const geo = useMemo(() => {
    if (points.length < 2 || width < 20) return null;
    const t0 = points[0].t.getTime();
    const t1 = points[points.length - 1].t.getTime();
    const vmax = Math.max(...points.map((p) => p.v), threshold ?? 0) * 1.1 || 1;
    const vmin = Math.min(0, ...points.map((p) => p.v));
    const x = (t: Date) => pad + ((t.getTime() - t0) / Math.max(1, t1 - t0)) * (width - 2 * pad);
    const y = (v: number) => height - pad - ((v - vmin) / (vmax - vmin)) * (height - 2 * pad);
    const xs = points.map((p) => x(p.t));
    const d = points.map((p, i) => `${i ? "L" : "M"}${xs[i].toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    return { xs, y, d, thresholdY: threshold != null ? y(threshold) : null };
  }, [points, width, height, threshold]);

  const onMove = (e: PointerEvent<SVGRectElement>) => {
    if (!geo) return;
    const box = e.currentTarget.getBoundingClientRect();
    setHover(nearestIndex(geo.xs, e.clientX - box.left));
  };
  const onKey = (e: KeyboardEvent) => {
    if (!geo) return;
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? points.length - 1) - 1));
    if (e.key === "ArrowRight") setHover((h) => Math.min(points.length - 1, (h ?? points.length - 1) + 1));
  };
  const last = points[points.length - 1];
  const shown = hover != null ? points[hover] : null;

  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {geo ? (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={`${label}: ${points.length} readings, latest ${format(last.v)}`}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => setHover(null)}
          className="block"
        >
          {geo.thresholdY != null && (
            <line x1={pad} x2={width - pad} y1={geo.thresholdY} y2={geo.thresholdY} stroke="var(--axis)" strokeWidth={1} />
          )}
          <path d={geo.d} fill="none" stroke="var(--data)" strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
          <circle cx={geo.xs[geo.xs.length - 1]} cy={geo.y(last.v)} r={3.5} fill={toneVar(tone)} stroke="var(--panel)" strokeWidth={2} />
          {shown && hover != null && (
            <g>
              <line x1={geo.xs[hover]} x2={geo.xs[hover]} y1={0} y2={height} stroke="var(--axis)" strokeWidth={1} />
              <circle cx={geo.xs[hover]} cy={geo.y(shown.v)} r={3.5} fill="var(--data)" stroke="var(--panel)" strokeWidth={2} />
            </g>
          )}
          <rect
            x={0}
            y={0}
            width={width}
            height={height}
            fill="transparent"
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
      ) : (
        <div className="flex h-full items-center text-[12px] text-muted">collecting…</div>
      )}
      {shown && hover != null && geo && (
        <Tooltip x={geo.xs[hover]} width={width}>
          <div className="mono font-semibold text-text">{format(shown.v)}</div>
          <div className="text-muted">{fmtClock(shown.t)}</div>
        </Tooltip>
      )}
    </div>
  );
}

// ------------------------------------------------------------ burn rate --
export interface BurnPoint {
  t: Date;
  long: number | null;
  short: number | null;
}

const THRESHOLDS = [
  { value: 14.4, tone: "critical" as Tone, label: "14.4× fast burn (page)" },
  { value: 6, tone: "warn" as Tone, label: "6× medium burn (ticket)" },
  { value: 1, tone: "neutral" as Tone, label: "1× budget-neutral" },
];

/** symlog: keeps 0 on the axis while spreading 1×, 6×, 14.4× and 1000× apart. */
const slog = (v: number) => Math.log10(1 + Math.max(0, v));

/**
 * The FAST_BURN pair over time — 1h (the alert's long window, primary ink)
 * and 5m (its confirmation window, de-emphasised) — against the three
 * multiwindow thresholds. One y-axis, log scale.
 */
export function BurnRateChart({ points, height = 190 }: { points: BurnPoint[]; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const clipId = useId();
  const m = { top: 10, right: 12, bottom: 22, left: 44 };
  const geo = useMemo(() => {
    if (points.length < 2 || width < 120) return null;
    const observed = points.flatMap((p) => [p.long ?? 0, p.short ?? 0]);
    const top = Math.max(24, Math.max(...observed) * 1.25);
    const innerW = width - m.left - m.right;
    const innerH = height - m.top - m.bottom;
    const t0 = points[0].t.getTime();
    const t1 = points[points.length - 1].t.getTime();
    const x = (t: Date) => m.left + ((t.getTime() - t0) / Math.max(1, t1 - t0)) * innerW;
    const y = (v: number) => m.top + innerH - (slog(v) / slog(top)) * innerH;
    const xs = points.map((p) => x(p.t));
    const line = (key: "long" | "short") => {
      let d = "";
      let pen = false;
      points.forEach((p, i) => {
        const v = p[key];
        if (v == null) {
          pen = false;
          return;
        }
        d += `${pen ? "L" : "M"}${xs[i].toFixed(1)},${y(v).toFixed(1)} `;
        pen = true;
      });
      return d;
    };
    const ticks = [0, 1, 6, 14.4, 100, 1000, 10000].filter((v) => v <= top);
    const tStep = Math.max(1, Math.floor(points.length / 4));
    const timeTicks = points.filter((_, i) => i % tStep === 0 && i < points.length - tStep / 2);
    return { x, y, xs, long: line("long"), short: line("short"), ticks, timeTicks, innerW, innerH };
  }, [points, width, height, m.left, m.right, m.top, m.bottom]);

  const onMove = (e: PointerEvent<SVGRectElement>) => {
    if (!geo) return;
    const box = e.currentTarget.getBoundingClientRect();
    setHover(nearestIndex(geo.xs, e.clientX - box.left + m.left));
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? points.length - 1) - 1));
    if (e.key === "ArrowRight") setHover((h) => Math.min(points.length - 1, (h ?? points.length - 1) + 1));
  };
  const shown = hover != null ? points[hover] : null;
  const last = points[points.length - 1];

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-text-2">
        <span className="inline-flex items-center gap-1.5">
          <svg width="16" height="6" aria-hidden><line x1="0" y1="3" x2="16" y2="3" stroke="var(--data)" strokeWidth="2" /></svg>
          1h window
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="16" height="6" aria-hidden><line x1="0" y1="3" x2="16" y2="3" stroke="var(--data-2)" strokeWidth="1.5" /></svg>
          5m window
        </span>
        <span className="ml-auto text-muted">log scale · burn = bad ratio ÷ (1 − SLO)</span>
      </div>
      <div ref={ref} className="relative w-full" style={{ height }}>
        {geo ? (
          <svg
            width={width}
            height={height}
            role="img"
            tabIndex={0}
            onKeyDown={onKey}
            onBlur={() => setHover(null)}
            aria-label={`Burn rate over time. Latest 1h ${fmtBurn(last.long)}, 5m ${fmtBurn(last.short)}.`}
            className="block"
          >
            <defs>
              <clipPath id={clipId}>
                <rect x={m.left} y={m.top} width={geo.innerW} height={geo.innerH} />
              </clipPath>
            </defs>
            {geo.ticks.map((v) => (
              <g key={v}>
                <line x1={m.left} x2={width - m.right} y1={geo.y(v)} y2={geo.y(v)} stroke="var(--grid)" strokeWidth={1} />
                <text x={m.left - 6} y={geo.y(v) + 3.5} textAnchor="end" className="mono num" fontSize="10" fill="var(--muted)">
                  {v === 0 ? "0" : `${v}×`}
                </text>
              </g>
            ))}
            {THRESHOLDS.map((th) => (
              <line
                key={th.value}
                x1={m.left}
                x2={width - m.right}
                y1={geo.y(th.value)}
                y2={geo.y(th.value)}
                stroke={toneVar(th.tone === "neutral" ? "neutral" : th.tone)}
                strokeOpacity={th.tone === "neutral" ? 0.6 : 0.9}
                strokeWidth={1}
                strokeDasharray="4 4"
              />
            ))}
            <g clipPath={`url(#${clipId})`}>
              <path d={geo.short} fill="none" stroke="var(--data-2)" strokeWidth={1.5} strokeLinejoin="round" />
              <path d={geo.long} fill="none" stroke="var(--data)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            </g>
            {geo.timeTicks.map((p) => (
              <text key={p.t.getTime()} x={geo.x(p.t)} y={height - 6} textAnchor="middle" className="mono num" fontSize="10" fill="var(--muted)">
                {p.t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </text>
            ))}
            {shown && hover != null && (
              <g>
                <line x1={geo.xs[hover]} x2={geo.xs[hover]} y1={m.top} y2={m.top + geo.innerH} stroke="var(--axis)" strokeWidth={1} />
                {shown.long != null && (
                  <circle cx={geo.xs[hover]} cy={geo.y(shown.long)} r={4} fill="var(--data)" stroke="var(--panel)" strokeWidth={2} />
                )}
              </g>
            )}
            <rect
              x={m.left}
              y={m.top}
              width={geo.innerW}
              height={geo.innerH}
              fill="transparent"
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
            />
          </svg>
        ) : (
          <div className="flex h-full items-center justify-center text-[12px] text-muted">waiting for SLI samples…</div>
        )}
        {shown && hover != null && geo && (
          <Tooltip x={geo.xs[hover]} width={width}>
            <div className="text-muted">{fmtClock(shown.t)}</div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-text-2">1h</span>
              <span className="mono font-semibold text-text">{fmtBurn(shown.long)}</span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-text-2">5m</span>
              <span className="mono font-semibold text-text">{fmtBurn(shown.short)}</span>
            </div>
          </Tooltip>
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-text-2">
        {THRESHOLDS.map((th) => (
          <li key={th.value} className="inline-flex items-center gap-1.5">
            <svg width="14" height="6" aria-hidden>
              <line x1="0" y1="3" x2="14" y2="3" stroke={toneVar(th.tone)} strokeWidth="1.5" strokeDasharray="3 2" />
            </svg>
            {th.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Service health at a glance: one segment per service, in its status colour. */
export function HealthRing({ statuses, size = 44 }: { statuses: Tone[]; size?: number }) {
  const r = size / 2 - 4;
  const c = 2 * Math.PI * r;
  const n = Math.max(1, statuses.length);
  const gap = n > 1 ? 3 : 0;
  const seg = c / n - gap;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden className="shrink-0 -rotate-90">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--grid)" strokeWidth={5} />
      {statuses.map((s, i) => (
        <circle
          key={i}
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={toneVar(s)}
          strokeWidth={5}
          strokeDasharray={`${Math.max(0.5, seg)} ${c - Math.max(0.5, seg)}`}
          strokeDashoffset={-(i * (c / n))}
        />
      ))}
    </svg>
  );
}
