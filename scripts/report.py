#!/usr/bin/env python3
"""Print the reliability report for a window: one command, no dashboard.

    python3 scripts/report.py                 # last 7 days, for a terminal
    python3 scripts/report.py --days 30
    python3 scripts/report.py --markdown > report.md

Reads GET /analytics/summary, so it reports what the system measured rather
than anything this script works out for itself. A figure the window cannot
support prints as a dash, never as zero.
"""

from __future__ import annotations

import argparse
import sys

from _api import ApiError, get

DASH = "—"


def pct(x: float | None, digits: int = 1) -> str:
    return DASH if x is None else f"{x * 100:.{digits}f}%"


def secs(x: float | None) -> str:
    if x is None:
        return DASH
    if x < 90:
        return f"{x:.1f}s"
    return f"{int(x // 60)}m {int(x % 60):02d}s"


def money(x: float | None) -> str:
    return DASH if x is None else f"${x:,.2f}"


def num(x: float | None) -> str:
    return DASH if x is None else f"{x:,}"


def budget(used: float | None) -> str:
    if used is None:
        return DASH
    return "spent" if used > 1 else pct(used, 0)


def verdict(summary: dict) -> str:
    """One line: the thing to say first in a review."""
    failure_rate = summary["delivery"]["change_failure_rate"]
    reliability = summary["reliability"]
    held = reliability["services_meeting_slo"]
    total = len(reliability["services"])
    if failure_rate is None and total == 0:
        return "Nothing shipped and nothing measured in this window."
    parts = []
    if failure_rate is not None:
        parts.append(f"{pct(failure_rate, 0)} of rollouts were taken back")
    if total:
        parts.append(f"{held} of {total} services held their objective")
    return " and ".join(parts) + "."


def render(summary: dict, markdown: bool) -> str:
    d, r, resp, c = summary["delivery"], summary["reliability"], summary["response"], summary["cost"]
    window = summary["window"]
    h1, h2, rule = ("# ", "## ", "") if markdown else ("", "", "-" * 64)
    out: list[str] = []

    out += [f"{h1}OpsForge reliability report", ""]
    out += [f"Window: last {window['days']:g} days, to {window['end'][:16].replace('T', ' ')}", ""]
    out += [f"**{verdict(summary)}**" if markdown else verdict(summary).upper(), ""]

    rows = [
        ("Releases", f"{d['releases']} ({num(d['per_day'])}/day)"),
        ("Rolled back", f"{d['rollbacks']}"),
        ("Change failure rate", pct(d["change_failure_rate"], 0)),
        ("Rollout duration", f"{secs(d['duration_p50_s'])} median, {secs(d['duration_p95_s'])} p95"),
    ]
    out += section("Delivery", rows, h2, rule, markdown)

    rows = [
        ("Fleet availability", pct(r["fleet_availability"], 3)),
        ("Requests measured", num(r["requests"])),
        ("Services meeting SLO", f"{r['services_meeting_slo']} of {len(r['services'])}"),
        ("Weakest service", r["worst_service"] or DASH),
    ]
    out += section("Reliability", rows, h2, rule, markdown)
    if r["services"]:
        out += table(
            ["Service", "Availability", "Objective", "Budget used"],
            [
                [
                    s["service"],
                    pct(s["availability"], 3),
                    pct(s["slo_target"], 1),
                    budget(s["budget_consumed"]),
                ]
                for s in sorted(r["services"], key=lambda s: s["availability"] if s["availability"] is not None else 1)
            ],
            markdown,
        )

    caught = ", ".join(f"{k} ({v})" for k, v in resp["detection_sources"].items()) or DASH
    rows = [
        ("Incidents", f"{resp['incidents']} ({resp['root_incidents']} root, {resp['cascading_incidents']} cascading)"),
        ("Time to restore", f"{secs(resp['mttr_s'])} mean, {secs(resp['duration_p95_s'])} p95"),
        ("Time to detect", f"{secs(resp['mttd_s'])} (measured by drills)"),
        ("Drills passed", DASH if not resp["drills"] else f"{resp['drills_passed']} of {resp['drills']}"),
        ("Caught first by", caught),
    ]
    out += section("Response", rows, h2, rule, markdown)

    rows = [
        ("Estimated impact", money(c["total_value"])),
        ("Incidents priced", f"{c['priced_incidents']}"),
        ("Costliest service", c["worst_service"] or DASH),
    ]
    out += section("Cost", rows, h2, rule, markdown)
    if c["by_service"]:
        out += table(
            ["Service", "Impact", "Incidents"],
            [[s["service"], money(s["value"]), str(s["incidents"])] for s in c["by_service"]],
            markdown,
        )

    out += ["", "Revenue per request is a configurable parameter; every input is stored with the figure."]
    return "\n".join(out)


def section(title: str, rows: list[tuple[str, str]], h2: str, rule: str, markdown: bool) -> list[str]:
    out = [f"{h2}{title}" if markdown else f"{rule}\n{title.upper()}", ""]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        out.append(f"- **{label}**: {value}" if markdown else f"  {label.ljust(width)}   {value}")
    out.append("")
    return out


def table(headers: list[str], rows: list[list[str]], markdown: bool) -> list[str]:
    if markdown:
        return (
            ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            + ["| " + " | ".join(r) + " |" for r in rows]
            + [""]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True))

    return [line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows] + [""]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--markdown", action="store_true", help="emit Markdown instead of terminal text")
    args = ap.parse_args()
    try:
        summary = get("/analytics/summary", days=args.days)
    except (ApiError, OSError) as exc:
        print(f"Could not reach the backend: {exc}", file=sys.stderr)
        return 1
    print(render(summary, args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
