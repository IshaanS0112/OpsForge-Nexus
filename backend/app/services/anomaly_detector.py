"""Layer 1 of the anomaly suite: rolling z-score.

Design note: an adaptive z-score per metric instead of a fixed threshold, so
each service is judged against *its own* baseline without per-service tuning.
Known tradeoff: it needs enough history for a stable baseline, and it misreads
recurring patterns. A 100-reading window adapts to a smooth daily curve, but a
sharp recurring event — the batch job that runs every night at 02:00, the
9 a.m. login storm — lands far above the recent window every single time. The
seasonal layer exists to fix exactly that.

Rule (unchanged from V1): rolling window of the last N=100 readings,
|z| > 3.0, K=3 consecutive readings.

Three refinements over V1, all about the baseline rather than the rule:

* Variance floor. A perfectly flat baseline (a healthy service's error rate
  is 0.0 every tick) has sigma = 0, so z is undefined and V1 could never
  flag an error burst on a service that had never errored. ``sigma`` is
  floored per metric so a jump off a flat line still registers.
* Clean baseline. Readings flagged anomalous are kept out of the rolling
  window, so an ongoing incident does not inflate sigma and talk the detector
  out of its own alarm. After ``ADAPT_AFTER`` consecutive exclusions the
  window admits new readings again: a level shift that persists that long is
  the new normal, not an anomaly.
* One-sided. All four metrics are "higher is worse", so only upward
  deviations are flagged. A drop is often a symptom elsewhere: when payments
  fails, checkout fails fast and stops calling inventory, so catalog's CPU
  falls — a real deviation, but not catalog's problem. (Drops are still kept
  out of the baseline, like spikes.)

The pure functions (``zscore``, ``AnomalyStreak``) are DB-free and unit
tested in isolation; ``AnomalyDetectorService`` serves the V1 single-metric
ingest path and the RCA signal collector.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.models import Metric

logger = logging.getLogger(__name__)

METRICS = ("error_rate", "latency_p95", "cpu_usage", "memory_usage")
# (absolute floor, floor relative to |mean|) for sigma, per metric.
SIGMA_FLOORS: dict[str, tuple[float, float]] = {
    "error_rate": (0.01, 0.0),  # one percentage point
    "latency_p95": (2.0, 0.05),  # 2 ms or 5% of the mean
    "cpu_usage": (0.01, 0.05),
    "memory_usage": (0.005, 0.02),
}
ADAPT_AFTER = 60  # consecutive excluded readings before the baseline adapts


def zscore(current_value: float, window: list[float]) -> float | None:
    """Return the z-score of ``current_value`` against ``window``.

    Returns ``None`` when the baseline is not yet established (too few points
    or zero variance) — the caller treats that as "not anomalous, still warming
    up" rather than raising a false positive.
    """
    if len(window) < 2:
        return None
    mu = statistics.fmean(window)
    sigma = statistics.pstdev(window)
    if sigma == 0:
        return None
    return (current_value - mu) / sigma


def floored_zscore(current_value: float, window: list[float], metric: str, min_points: int = 20) -> float | None:
    """z-score with a per-metric sigma floor. None until ``min_points``
    readings exist (warm-up)."""
    if len(window) < min_points:
        return None
    mu = statistics.fmean(window)
    sigma = statistics.pstdev(window)
    absolute, relative = SIGMA_FLOORS.get(metric, (1e-9, 0.0))
    sigma = max(sigma, absolute, relative * abs(mu))
    return (current_value - mu) / sigma


@dataclass
class AnomalyStreak:
    """Tracks consecutive anomalous readings per (service, metric).

    An incident is signalled only after K consecutive anomalies, which
    suppresses one-off spikes from tripping the pipeline.
    """

    z_threshold: float = settings.anomaly_z_threshold
    consecutive_needed: int = settings.anomaly_consecutive_to_incident
    _counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def observe(self, service: str, metric: str, z: float | None) -> bool:
        """Feed a new z-score. Returns True when the streak crosses K -> incident."""
        key = (service, metric)
        if z is not None and abs(z) > self.z_threshold:
            self._counts[key] = self._counts.get(key, 0) + 1
        else:
            self._counts[key] = 0  # streak broken
        return self._counts[key] >= self.consecutive_needed

    def count(self, service: str, metric: str) -> int:
        return self._counts.get((service, metric), 0)

    def reset(self, service: str, metric: str) -> None:
        self._counts.pop((service, metric), None)


@dataclass
class LayerResult:
    """One detector layer's verdict for one service on one tick."""

    layer: str
    flagged: bool
    warm: bool  # False while the layer lacks history to judge
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"layer": self.layer, "flagged": self.flagged, "warm": self.warm, **self.details}


class ZScoreLayer:
    """In-memory rolling windows for the control loop (one reading per metric
    per tick)."""

    name = "zscore"

    def __init__(
        self,
        window: int = settings.anomaly_window_size,
        threshold: float = settings.anomaly_z_threshold,
        k: int = settings.anomaly_consecutive_to_incident,
    ) -> None:
        self.threshold = threshold
        self._windows: dict[tuple[str, str], deque] = {}
        self._excluded_run: dict[tuple[str, str], int] = {}
        self._maxlen = window
        self.streak = AnomalyStreak(z_threshold=threshold, consecutive_needed=k)

    def seed(self, service: str, metric: str, values: list[float]) -> None:
        self._windows[(service, metric)] = deque(values[-self._maxlen :], maxlen=self._maxlen)

    def observe(
        self, service: str, values: dict[str, Optional[float]]
    ) -> tuple[LayerResult, dict[str, Optional[float]]]:
        zs: dict[str, Optional[float]] = {}
        streaks = {}
        warm = False
        flagged = False
        for metric in METRICS:
            value = values.get(metric)
            if value is None:
                zs[metric] = None
                continue
            key = (service, metric)
            window = self._windows.setdefault(key, deque(maxlen=self._maxlen))
            z = floored_zscore(value, list(window), metric)
            zs[metric] = z
            warm = warm or z is not None
            upward = max(z, 0.0) if z is not None else None  # one-sided: higher is worse
            hit = self.streak.observe(service, metric, upward)
            streaks[metric] = self.streak.count(service, metric)
            flagged = flagged or hit
            # Flag one-sided, but keep the baseline clean on both sides: a
            # transient drop would otherwise inflate sigma just as a spike does.
            anomalous = z is not None and abs(z) > self.threshold
            run = self._excluded_run.get(key, 0)
            if anomalous and run < ADAPT_AFTER:
                self._excluded_run[key] = run + 1  # keep the baseline clean
            else:
                self._excluded_run[key] = 0
                window.append(value)
        details = {
            "z": {m: round(z, 2) for m, z in zs.items() if z is not None},
            "streak": streaks,
            "threshold": self.threshold,
        }
        return LayerResult(self.name, flagged, warm, details), zs


class AnomalyDetectorService:
    """Persistence-backed z-score for the V1 single-metric ingest path and the
    RCA signal collector."""

    def __init__(self, streak: AnomalyStreak | None = None) -> None:
        self.streak = streak or AnomalyStreak()

    def _recent_window(self, db: Session, service: str, metric: str) -> list[float]:
        stmt = (
            select(Metric.value)
            .where(Metric.service_name == service, Metric.metric_name == metric)
            .order_by(Metric.recorded_at.desc())
            .limit(settings.anomaly_window_size)
        )
        # newest-first; exclude the just-inserted point handled by caller
        return [row[0] for row in db.execute(stmt).all()]

    def ingest(self, db: Session, service: str, metric: str, value: float) -> tuple[Metric, bool]:
        """Persist a metric point, compute its z-score, and report whether its
        streak has reached K. Returns (metric_row, streak_reached)."""
        window = self._recent_window(db, service, metric)  # history BEFORE this point
        z = zscore(value, window)

        row = Metric(service_name=service, metric_name=metric, value=value, z_score=z, recorded_at=utcnow())
        db.add(row)
        db.commit()
        db.refresh(row)

        streak_reached = self.streak.observe(service, metric, z)
        if z is not None and abs(z) > settings.anomaly_z_threshold:
            logger.info("Anomalous %s/%s value=%.4f z=%.2f", service, metric, value, z)
        return row, streak_reached

    def metric_anomalies(self, db: Session, service: str, since: datetime) -> list[dict]:
        """Structured anomaly list for the RCA signal collector."""
        stmt = (
            select(Metric)
            .where(
                Metric.service_name == service,
                Metric.recorded_at >= since,
                Metric.z_score.isnot(None),
            )
            .order_by(Metric.recorded_at.desc())
        )
        out: dict[str, dict] = {}
        onset: dict[str, datetime] = {}  # first anomalous reading per metric
        for m in db.execute(stmt).scalars():
            # One-sided, like the detector layers: every metric is "higher is worse".
            if m.z_score <= settings.anomaly_z_threshold:
                continue
            onset[m.metric_name] = m.recorded_at  # newest-first, so this ends at the earliest
            # keep the strongest anomaly per metric within the window
            prev = out.get(m.metric_name)
            if prev is None or m.z_score > prev["z_score"]:
                out[m.metric_name] = {
                    "metric": m.metric_name,
                    "z_score": round(m.z_score, 2),
                    "value": m.value,
                    "seasonal_residual_z": round(m.seasonal_residual_z, 2)
                    if m.seasonal_residual_z is not None
                    else None,
                }
        for metric, entry in out.items():
            baseline = self._baseline(db, service, metric, before=onset[metric])
            entry["baseline"] = round(baseline, 4) if baseline is not None else None
        return list(out.values())

    @staticmethod
    def _baseline(db: Session, service: str, metric: str, before: datetime) -> Optional[float]:
        """What the metric looked like before the anomaly began: the mean of
        the readings before its onset that were not themselves anomalous. The
        latest readings would include the incident and understate the jump."""
        stmt = (
            select(Metric.value)
            .where(
                Metric.service_name == service,
                Metric.metric_name == metric,
                Metric.recorded_at < before,
                or_(Metric.z_score.is_(None), Metric.z_score <= settings.anomaly_z_threshold),
            )
            .order_by(Metric.recorded_at.desc())
            .limit(settings.anomaly_window_size)
        )
        values = [row[0] for row in db.execute(stmt).all()]
        return statistics.fmean(values) if values else None


def default_since() -> datetime:
    return utcnow() - timedelta(minutes=settings.rca_lookback_minutes)
