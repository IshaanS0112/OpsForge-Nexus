"""SLO error-budget burn-rate alerting.

Method: Google SRE Workbook, chapter 5 "Alerting on SLOs" — the multiwindow,
multi-burn-rate alert (the chapter's recommended configuration):

    burn_rate = bad_ratio / (1 - SLO_target)

A burn rate of 1 spends exactly the error budget over the SLO period; 14.4
spends 2% of a 30-day budget in one hour. Each alert has a long window (is
the budget really being spent?) and a short window (is it *still* being
spent?). It fires only when BOTH exceed the threshold, so one bad spike does
not page and the alert resets soon after recovery.

    FAST_BURN    > 14.4   1h, confirmed by 5m    -> page, rollback-eligible
    MEDIUM_BURN  > 6      6h, confirmed by 30m   -> ticket
    SLOW_BURN    > 1      3d, confirmed by 6h    -> weekly review

The SLI is request-based: a request is bad if it returned 5xx, failed at the
transport, or took longer than the service's latency threshold. Counts come
from ``sli_samples``, written by the control loop from gateway measurements.

Windows are summed over the data that exists; a freshly started system has
less than a full long window, which makes its long-window burn rates react
faster, not slower. ``coverage_s`` reports how much of each window has data.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BurnRateAlert, SLISample, SLOConfig


@dataclass(frozen=True)
class BurnPolicy:
    severity: str
    long_window: timedelta
    long_label: str
    short_window: timedelta
    short_label: str
    threshold: float
    action: str


POLICY: tuple[BurnPolicy, ...] = (
    BurnPolicy("FAST_BURN", timedelta(hours=1), "1h", timedelta(minutes=5), "5m", 14.4, "page / rollback-eligible"),
    BurnPolicy("MEDIUM_BURN", timedelta(hours=6), "6h", timedelta(minutes=30), "30m", 6.0, "ticket"),
    BurnPolicy("SLOW_BURN", timedelta(days=3), "3d", timedelta(hours=6), "6h", 1.0, "weekly review"),
)
SEVERITY_RANK = {p.severity: i for i, p in enumerate(POLICY)}  # 0 = most severe


@dataclass
class WindowCounts:
    total: int = 0
    bad: int = 0
    first_at: Optional[datetime] = None  # earliest sample in the window


@dataclass
class AlertEvaluation:
    severity: str
    long_window: str
    short_window: str
    threshold: float
    long_burn: Optional[float]
    short_burn: Optional[float]
    long_requests: int
    short_requests: int
    coverage_s: float  # seconds of the long window that have data
    firing: bool
    budget_fraction_at_threshold: float  # share of the SLO-period budget the long window spends at threshold

    def to_dict(self) -> dict:
        return asdict(self)


def burn_rate(bad: int, total: int, slo_target: float) -> Optional[float]:
    """bad_ratio / (1 - SLO). None when there is no traffic to judge."""
    if not 0 < slo_target < 1:
        raise ValueError("slo_target must be in (0, 1)")
    if total <= 0:
        return None
    return (bad / total) / (1.0 - slo_target)


def budget_fraction(policy: BurnPolicy, window_days: int) -> float:
    """Fraction of the SLO-period error budget spent when the long window
    burns at exactly the threshold (2% / 5% / 10% for a 30-day SLO)."""
    return policy.threshold * policy.long_window / timedelta(days=window_days)


def evaluate_policy(
    counts_for: Callable[[timedelta], WindowCounts],
    slo_target: float,
    now: datetime,
    *,
    window_days: int = 30,
    min_requests: int = settings.burn_min_requests,
) -> list[AlertEvaluation]:
    """Pure: evaluate every alert in POLICY. ``counts_for(window)`` returns
    the SLI counts over [now - window, now]."""
    out = []
    for p in POLICY:
        long_c, short_c = counts_for(p.long_window), counts_for(p.short_window)
        lb = burn_rate(long_c.bad, long_c.total, slo_target)
        sb = burn_rate(short_c.bad, short_c.total, slo_target)
        firing = (
            lb is not None
            and sb is not None
            and short_c.total >= min_requests
            and lb > p.threshold
            and sb > p.threshold
        )
        coverage = 0.0
        if long_c.first_at is not None:
            coverage = min(p.long_window.total_seconds(), (now - long_c.first_at).total_seconds())
        out.append(
            AlertEvaluation(
                severity=p.severity,
                long_window=p.long_label,
                short_window=p.short_label,
                threshold=p.threshold,
                long_burn=lb,
                short_burn=sb,
                long_requests=long_c.total,
                short_requests=short_c.total,
                coverage_s=round(coverage, 1),
                firing=firing,
                budget_fraction_at_threshold=round(budget_fraction(p, window_days), 4),
            )
        )
    return out


def worst_firing(evals: list[AlertEvaluation]) -> Optional[AlertEvaluation]:
    firing = [e for e in evals if e.firing]
    return min(firing, key=lambda e: SEVERITY_RANK[e.severity]) if firing else None


# ------------------------------------------------------------- canary gate --
def wilson_lower_bound(bad: int, total: int, z: float = 1.96) -> float:
    """Lower end of the Wilson score interval for the bad ratio (95% by
    default). Small samples get wide intervals, so a single slow request in
    twenty cannot by itself prove a canary is burning budget."""
    if total <= 0:
        return 0.0
    p = bad / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


@dataclass
class GateVerdict:
    verdict: str  # PASS | FAIL | WAIT
    reason: str
    canary_requests: int
    canary_bad: int
    canary_burn: Optional[float]
    canary_burn_lower_bound: Optional[float]
    stable_requests: int
    stable_bad: int
    stable_burn: Optional[float]
    threshold: float
    min_requests: int
    elapsed_s: float
    window_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_canary_gate(
    canary: WindowCounts,
    stable: WindowCounts,
    slo_target: float,
    *,
    elapsed_s: float,
    window_s: float = settings.canary_stage_window_s,
    threshold: float = settings.canary_max_burn_rate,
    min_requests: int = settings.canary_min_requests,
    max_wait_s: float = settings.canary_max_stage_wait_s,
) -> GateVerdict:
    """Gate one canary stage on the canary colour's own traffic.

    - FAIL, at any point in the window, once we are 95% confident the canary
      burns budget faster than the threshold: the Wilson lower bound of its
      bad ratio, divided by the error budget, exceeds it. A handful of
      requests at 5% exposure cannot convict a canary on one slow response;
      each later stage sends it more traffic, so the test gains power as
      exposure grows.
    - PASS once the confirmation window has elapsed with at least
      ``min_requests`` canary requests and no FAIL.
    - Too little canary traffic for ``max_wait_s`` fails the stage: an
      unmeasured canary is not a healthy one.

    The stable colour's burn rate is recorded alongside for context; the
    verdict does not depend on it (a breach during a rollout aborts the
    rollout whatever its cause)."""
    cb = burn_rate(canary.bad, canary.total, slo_target)
    sb = burn_rate(stable.bad, stable.total, slo_target)
    budget = 1.0 - slo_target
    lower = wilson_lower_bound(canary.bad, canary.total) / budget if canary.total else None

    def verdict(v: str, reason: str) -> GateVerdict:
        return GateVerdict(
            verdict=v,
            reason=reason,
            canary_requests=canary.total,
            canary_bad=canary.bad,
            canary_burn=cb,
            canary_burn_lower_bound=lower,
            stable_requests=stable.total,
            stable_bad=stable.bad,
            stable_burn=sb,
            threshold=threshold,
            min_requests=min_requests,
            elapsed_s=round(elapsed_s, 1),
            window_s=window_s,
        )

    stable_note = ""
    if sb is not None and sb > threshold:
        stable_note = f"; stable colour also burning {sb:.1f}x, so the degradation is not specific to the canary"

    if lower is not None and lower > threshold:
        return verdict(
            "FAIL",
            f"canary burn rate {cb:.1f}x exceeds {threshold}x with 95% confidence "
            f"({canary.bad}/{canary.total} bad){stable_note}",
        )
    if elapsed_s < window_s:
        return verdict("WAIT", f"confirmation window {elapsed_s:.0f}/{window_s:.0f}s")
    if canary.total < min_requests:
        if elapsed_s >= max_wait_s:
            return verdict(
                "FAIL", f"insufficient canary traffic: {canary.total}/{min_requests} requests in {elapsed_s:.0f}s"
            )
        return verdict("WAIT", f"waiting for canary traffic: {canary.total}/{min_requests} requests")
    return verdict(
        "PASS",
        f"canary burn rate {cb:.2f}x (95% lower bound {lower:.2f}x) within {threshold}x over {canary.total} requests",
    )


# ------------------------------------------------------------- persistence --
@dataclass
class ServiceBurn:
    service: str
    slo_target: float
    window_days: int
    latency_threshold_ms: float
    evaluations: list[AlertEvaluation]
    worst: Optional[str]
    budget_consumed: Optional[float]  # fraction of the SLO-period budget spent (data that exists)
    budget_remaining: Optional[float]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evaluations"] = [e.to_dict() for e in self.evaluations]
        return d


class BurnRateService:
    def slo(self, db: Session, service: str) -> SLOConfig:
        cfg = db.get(SLOConfig, service)
        if cfg is None:
            cfg = SLOConfig(
                service_name=service,
                slo_target=settings.slo_default_target,
                window_days=settings.slo_default_window_days,
                latency_threshold_ms=settings.slo_default_latency_ms,
            )
        return cfg

    def window_counts(
        self, db: Session, service: str, since: datetime, until: datetime, color: Optional[str] = None
    ) -> WindowCounts:
        cond = [SLISample.service_name == service, SLISample.window_end > since, SLISample.window_end <= until]
        if color is not None:
            cond.append(SLISample.color == color)
        row = db.execute(
            select(
                func.coalesce(func.sum(SLISample.total_requests), 0),
                func.coalesce(func.sum(SLISample.bad_requests), 0),
                func.min(SLISample.window_start),
            ).where(and_(*cond))
        ).one()
        return WindowCounts(total=int(row[0]), bad=int(row[1]), first_at=row[2])

    def evaluate(self, db: Session, service: str, now: datetime) -> ServiceBurn:
        cfg = self.slo(db, service)
        evals = evaluate_policy(
            lambda w: self.window_counts(db, service, now - w, now),
            cfg.slo_target,
            now,
            window_days=cfg.window_days,
        )
        period = self.window_counts(db, service, now - timedelta(days=cfg.window_days), now)
        consumed = None
        if period.total:
            consumed = (period.bad / period.total) / (1.0 - cfg.slo_target)
            # Spend so far relative to the whole period's allowance: the ratio
            # above is the average burn rate; scale by elapsed share of period.
            elapsed = (now - period.first_at).total_seconds() if period.first_at else 0.0
            consumed *= min(1.0, elapsed / timedelta(days=cfg.window_days).total_seconds())
        worst = worst_firing(evals)
        return ServiceBurn(
            service=service,
            slo_target=cfg.slo_target,
            window_days=cfg.window_days,
            latency_threshold_ms=cfg.latency_threshold_ms,
            evaluations=evals,
            worst=worst.severity if worst else None,
            budget_consumed=round(consumed, 5) if consumed is not None else None,
            budget_remaining=round(1.0 - consumed, 5) if consumed is not None else None,
        )

    def record_transitions(self, db: Session, burn: ServiceBurn, now: datetime) -> list[BurnRateAlert]:
        """Open an alert row when a severity starts firing; stamp
        ``resolved_at`` when it stops. Returns newly fired alerts."""
        open_rows = {
            a.severity: a
            for a in db.execute(
                select(BurnRateAlert).where(
                    BurnRateAlert.service_name == burn.service, BurnRateAlert.resolved_at.is_(None)
                )
            ).scalars()
        }
        fired = []
        for e in burn.evaluations:
            row = open_rows.get(e.severity)
            if e.firing and row is None:
                alert = BurnRateAlert(
                    service_name=burn.service,
                    severity=e.severity,
                    long_window=e.long_window,
                    short_window=e.short_window,
                    burn_rate=round(e.long_burn or 0.0, 3),
                    short_burn_rate=round(e.short_burn or 0.0, 3),
                    fired_at=now,
                )
                db.add(alert)
                fired.append(alert)
            elif not e.firing and row is not None:
                row.resolved_at = now
        return fired

    def gate(
        self,
        db: Session,
        service: str,
        canary_color: str,
        stable_color: str,
        since: datetime,
        now: datetime,
        *,
        window_s: float = settings.canary_stage_window_s,
    ) -> GateVerdict:
        cfg = self.slo(db, service)
        return evaluate_canary_gate(
            self.window_counts(db, service, since, now, color=canary_color),
            self.window_counts(db, service, since, now, color=stable_color),
            cfg.slo_target,
            elapsed_s=(now - since).total_seconds(),
            window_s=window_s,
        )
