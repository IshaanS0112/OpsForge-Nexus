"""Reliability analytics: the numbers an engineering or operations review asks
for, computed from the rows the control loop already writes.

Four questions, four sections:

* **Delivery** — how often releases ship, how many of them had to be taken back,
  how long one takes end to end. The first two are DORA's deployment frequency
  and change failure rate; the third is measured from the rollout itself.
* **Reliability** — measured availability per service against its SLO, and how
  much of the 30-day error budget the window consumed.
* **Response** — how incidents were first caught, how long they lasted, and how
  quickly injected faults were detected.
* **Cost** — the priced impact of those incidents, by service.

Every figure is derived, never stored: the source rows stay the record, so a
number can always be traced back to the deployments, incidents, SLI counts and
drills it came from. Where a window holds too little data to answer, the figure
is ``None`` rather than zero, because "nothing shipped" and "nothing measured"
are different answers.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BusinessImpact, ChaosExperiment, Deployment, Incident, SLISample, SLOConfig
from app.services.deployment_engine import DeployState

TERMINAL_STATES = (DeployState.LIVE.value, DeployState.ROLLED_BACK.value)

# How an incident was first opened, read from the reason the opener recorded.
DETECTION_SOURCES = (
    ("canary gate", "canary gate"),
    ("rollout failed", "rollout gate"),
    ("consecutive failed health checks", "health gate"),
    ("anomaly layers agree", "detector consensus"),
    ("FAST_BURN", "burn-rate page"),
)


def percentile(values: list[float], p: float) -> Optional[float]:
    """Nearest-rank percentile. None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def ratio(part: int, whole: int) -> Optional[float]:
    return part / whole if whole else None


def detection_source(trigger_reason: str) -> str:
    for marker, label in DETECTION_SOURCES:
        if marker in trigger_reason:
            return label
    return "other"


@dataclass
class Window:
    days: float
    start: datetime
    end: datetime

    def to_dict(self) -> dict:
        return {"days": self.days, "start": self.start.isoformat(), "end": self.end.isoformat()}


def delivery(db: Session, window: Window) -> dict:
    rows = list(
        db.execute(
            select(Deployment).where(
                Deployment.status.in_(TERMINAL_STATES),
                Deployment.deployed_at >= window.start,
                Deployment.deployed_at <= window.end,
            )
        ).scalars()
    )
    live = [d for d in rows if d.status == DeployState.LIVE.value]
    rolled_back = [d for d in rows if d.status == DeployState.ROLLED_BACK.value]
    durations = [(d.finished_at - d.deployed_at).total_seconds() for d in rows if d.finished_at and d.deployed_at]
    by_strategy: dict[str, dict[str, int]] = {}
    for d in rows:
        counts = by_strategy.setdefault(d.strategy, {"released": 0, "rolled_back": 0})
        counts["released" if d.status == DeployState.LIVE.value else "rolled_back"] += 1
    return {
        "releases": len(live),
        "rollbacks": len(rolled_back),
        "per_day": round(len(live) / window.days, 2) if window.days else None,
        "change_failure_rate": ratio(len(rolled_back), len(rows)),
        "duration_p50_s": percentile(durations, 50),
        "duration_p95_s": percentile(durations, 95),
        "by_strategy": by_strategy,
    }


def reliability(db: Session, window: Window) -> dict:
    counts: dict[str, list[int]] = {}
    for row in db.execute(
        select(SLISample).where(SLISample.window_end > window.start, SLISample.window_end <= window.end)
    ).scalars():
        totals = counts.setdefault(row.service_name, [0, 0])
        totals[0] += row.total_requests
        totals[1] += row.bad_requests

    targets = {c.service_name: c.slo_target for c in db.execute(select(SLOConfig)).scalars()}
    services = []
    for name, (total, bad) in sorted(counts.items()):
        target = targets.get(name, settings.slo_default_target)
        availability = ratio(total - bad, total)
        # The window is a slice of the SLO period, so its share of the budget is
        # the share of the period it covers.
        period_days = settings.slo_default_window_days
        budget_requests = total * (1 - target) * (period_days / window.days) if window.days else None
        services.append(
            {
                "service": name,
                "requests": total,
                "bad_requests": bad,
                "availability": availability,
                "slo_target": target,
                "meets_slo": None if availability is None else availability >= target,
                "budget_consumed": ratio(bad, round(budget_requests)) if budget_requests else None,
            }
        )
    worst = min((s for s in services if s["availability"] is not None), key=lambda s: s["availability"], default=None)
    total_requests = sum(s["requests"] for s in services)
    total_bad = sum(s["bad_requests"] for s in services)
    return {
        "services": services,
        "fleet_availability": ratio(total_requests - total_bad, total_requests),
        "requests": total_requests,
        "services_meeting_slo": sum(1 for s in services if s["meets_slo"]),
        "worst_service": worst["service"] if worst else None,
    }


def response(db: Session, window: Window) -> dict:
    incidents = list(
        db.execute(
            select(Incident).where(Incident.detected_at >= window.start, Incident.detected_at <= window.end)
        ).scalars()
    )
    resolved = [i for i in incidents if i.resolved_at]
    durations = [(i.resolved_at - i.detected_at).total_seconds() for i in resolved]

    sources: dict[str, int] = {}
    for i in incidents:
        label = detection_source(i.trigger_reason or "")
        sources[label] = sources.get(label, 0) + 1

    drills = list(
        db.execute(
            select(ChaosExperiment).where(
                ChaosExperiment.status == "COMPLETED",
                ChaosExperiment.injected_at >= window.start,
                ChaosExperiment.injected_at <= window.end,
            )
        ).scalars()
    )
    detections = [d.detection_latency_seconds for d in drills if d.detection_latency_seconds is not None]
    recoveries = [d.recovery_latency_seconds for d in drills if d.recovery_latency_seconds is not None]
    return {
        "incidents": len(incidents),
        "root_incidents": sum(1 for i in incidents if not i.is_cascading),
        "cascading_incidents": sum(1 for i in incidents if i.is_cascading),
        "resolved": len(resolved),
        "mttr_s": statistics.fmean(durations) if durations else None,
        "duration_p95_s": percentile(durations, 95),
        "detection_sources": dict(sorted(sources.items(), key=lambda kv: -kv[1])),
        "drills": len(drills),
        "drills_passed": sum(1 for d in drills if d.passed),
        # Only a drill knows when the fault actually started, so detection
        # latency is measured there and nowhere else.
        "mttd_s": statistics.fmean(detections) if detections else None,
        "recovery_p95_s": percentile(recoveries, 95),
    }


def cost(db: Session, window: Window) -> dict:
    rows = list(
        db.execute(
            select(BusinessImpact, Incident)
            .join(Incident, Incident.id == BusinessImpact.incident_id)
            .where(Incident.detected_at >= window.start, Incident.detected_at <= window.end)
        ).all()
    )
    per_service: dict[str, dict[str, float]] = {}
    for impact, incident in rows:
        entry = per_service.setdefault(
            incident.service_name, {"service": incident.service_name, "value": 0.0, "incidents": 0}
        )
        entry["value"] += impact.estimated_impact_value or 0.0
        entry["incidents"] += 1
    ranked = sorted(per_service.values(), key=lambda e: -e["value"])
    total = sum(e["value"] for e in ranked)
    return {
        "total_value": round(total, 2),
        "priced_incidents": len(rows),
        "revenue_per_request": settings.revenue_per_request,
        "by_service": [{**e, "value": round(e["value"], 2)} for e in ranked[:10]],
        "worst_service": ranked[0]["service"] if ranked else None,
    }


def summary(db: Session, now: datetime, days: float = 7.0) -> dict:
    window = Window(days=days, start=now - timedelta(days=days), end=now)
    return {
        "generated_at": now.isoformat(),
        "window": window.to_dict(),
        "delivery": delivery(db, window),
        "reliability": reliability(db, window),
        "response": response(db, window),
        "cost": cost(db, window),
    }
