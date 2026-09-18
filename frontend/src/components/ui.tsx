import type { ReactNode } from "react";
import type { Status } from "../api/client";
import { STATUS_META, type Tone } from "../lib/status";

const toneVar = (t: Tone) => (t === "neutral" ? "var(--muted)" : `var(--${t})`);

/**
 * One glyph per status, each a different shape, so state reads without
 * colour: check (ok), triangle (warn), octagon (critical), counter-clockwise
 * arrow (rollback), split arrow (canary).
 */
export function StatusIcon({ tone, size = 14 }: { tone: Tone; size?: number }) {
  const c = toneVar(tone);
  const common = { width: size, height: size, viewBox: "0 0 16 16", "aria-hidden": true } as const;
  switch (tone) {
    case "ok":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill={c} />
          <path d="M4.8 8.2l2.1 2.1 4.3-4.5" fill="none" stroke="var(--panel)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "warn":
      return (
        <svg {...common}>
          <path d="M8 1.5l6.8 12.2H1.2z" fill={c} strokeLinejoin="round" />
          <path d="M8 6v3.6" stroke="var(--panel)" strokeWidth="1.7" strokeLinecap="round" />
          <circle cx="8" cy="11.6" r="0.95" fill="var(--panel)" />
        </svg>
      );
    case "critical":
      return (
        <svg {...common}>
          <path d="M5.2 1.2h5.6l4 4v5.6l-4 4H5.2l-4-4V5.2z" fill={c} />
          <path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke="var(--panel)" strokeWidth="1.7" strokeLinecap="round" />
        </svg>
      );
    case "rollback":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill={c} />
          <path d="M5.2 6.2A3.4 3.4 0 1 1 5 9.6" fill="none" stroke="var(--panel)" strokeWidth="1.6" strokeLinecap="round" />
          <path d="M4.4 3.9v2.6H7" fill="none" stroke="var(--panel)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "canary":
      return (
        <svg {...common}>
          <rect x="1" y="1" width="14" height="14" rx="4" fill={c} />
          <path d="M4 8h3.2l3.6-3.2M7.2 8l3.6 3.2" fill="none" stroke="var(--panel)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="4" fill={c} />
        </svg>
      );
  }
}

export function StatusBadge({ tone, label, title }: { tone: Tone; label?: string; title?: string }) {
  const text = label ?? (tone === "neutral" ? "" : STATUS_META[tone as Status].label);
  return (
    <span
      title={title ?? (tone === "neutral" ? undefined : STATUS_META[tone as Status].hint)}
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[12px] font-semibold text-text"
      style={{
        borderColor: `color-mix(in oklab, ${toneVar(tone)} 45%, transparent)`,
        background: `color-mix(in oklab, ${toneVar(tone)} 12%, transparent)`,
      }}
    >
      <StatusIcon tone={tone} size={12} />
      {text}
    </span>
  );
}

export function Panel({
  title,
  eyebrow,
  actions,
  children,
  className = "",
  bodyClass = "p-4",
}: {
  title?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={`panel min-w-0 ${className}`}>
      {(title || eyebrow || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-[var(--ring)] px-4 py-3">
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-0.5">{eyebrow}</div>}
            {title && <h2 className="truncate text-[15px] font-bold tracking-tight">{title}</h2>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

/** Label / value pair. Values use proportional figures (they stand alone). */
export function Stat({ label, value, sub, mono = true }: { label: string; value: ReactNode; sub?: ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[12px] font-medium text-muted">{label}</div>
      <div className={`mt-0.5 truncate text-[18px] font-semibold ${mono ? "mono" : ""}`}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-[12px] text-text-2">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-grid px-4 py-6 text-center text-[13px] text-muted">{children}</div>;
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px]"
      style={{ borderColor: "color-mix(in oklab, var(--critical) 50%, transparent)" }}
    >
      <StatusIcon tone="critical" size={14} />
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

/** Small blue/green colour chip. Colour names are deployment colours, not status. */
export function ColorChip({ color }: { color: string | null | undefined }) {
  if (!color) return null;
  return (
    <span className="mono inline-flex items-center gap-1 rounded border border-grid px-1.5 text-[11px] text-text-2">
      <span className="inline-block h-1.5 w-1.5 rounded-full border border-[var(--muted)]" style={{ background: color === "blue" ? "transparent" : "var(--muted)" }} />
      {color}
    </span>
  );
}
