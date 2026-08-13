"""Z-score based anomaly detection.

Design note (interview): we use an adaptive z-score per metric instead of a
fixed threshold so each service is judged against *its own* baseline. This
avoids per-service manual tuning. Known tradeoff: it needs enough history for a
stable baseline and degrades under strong seasonality (daily traffic cycles).

The pure functions here (``zscore``, ``AnomalyStreak``) are DB-free and unit
tested in isolation; ``AnomalyDetectorService`` wires them to persistence.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Metric

logger = logging.getLogger(__name__)


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

    def reset(self, service: str, metric: str) -> None:
        self._counts.pop((service, metric), None)


class AnomalyDetectorService:
    """Persistence-backed detector used by the API layer."""

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
        """Persist a metric point, compute its z-score, and report if it should
        trigger incident creation. Returns (metric_row, should_create_incident)."""
        window = self._recent_window(db, service, metric)  # history BEFORE this point
        z = zscore(value, window)

        row = Metric(service_name=service, metric_name=metric, value=value, z_score=z)
        db.add(row)
        db.commit()
        db.refresh(row)

        should_incident = self.streak.observe(service, metric, z)
        if z is not None and abs(z) > settings.anomaly_z_threshold:
            logger.info("Anomalous %s/%s value=%.4f z=%.2f", service, metric, value, z)
        return row, should_incident

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
        for m in db.execute(stmt).scalars():
            if abs(m.z_score) <= settings.anomaly_z_threshold:
                continue
            # keep the strongest anomaly per metric within the window
            prev = out.get(m.metric_name)
            if prev is None or abs(m.z_score) > abs(prev["z_score"]):
                baseline_window = self._recent_window(db, service, m.metric_name)
                baseline = statistics.fmean(baseline_window) if baseline_window else None
                out[m.metric_name] = {
                    "metric": m.metric_name,
                    "z_score": round(m.z_score, 2),
                    "value": m.value,
                    "baseline": round(baseline, 4) if baseline is not None else None,
                }
        return list(out.values())


def default_since() -> datetime:
    return datetime.utcnow() - timedelta(minutes=settings.rca_lookback_minutes)
