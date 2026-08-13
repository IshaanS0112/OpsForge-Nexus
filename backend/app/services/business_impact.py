"""Business impact calculator.

Formula (assumptions are explicit and stored with every result):

    affected_requests   = COUNT(requests during incident window)
    error_rate_delta    = incident_period_error_rate - baseline_error_rate
    estimated_impact    = affected_requests * error_rate_delta * revenue_per_request

``revenue_per_request`` is a CONFIGURABLE business parameter (mock default
$0.50). Every input is stored in ``calculation_basis`` so the number is
auditable — "here is the formula and the inputs", not a black-box figure. In
production ``revenue_per_request`` would come from finance/analytics' real
conversion data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BusinessImpact, Incident, Log, Metric

logger = logging.getLogger(__name__)


def compute_impact(
    affected_requests: int,
    incident_error_rate: float,
    baseline_error_rate: float,
    revenue_per_request: float = settings.revenue_per_request,
) -> tuple[float, float]:
    """Pure function: returns (error_rate_delta, estimated_impact_value)."""
    error_rate_delta = max(0.0, incident_error_rate - baseline_error_rate)
    estimated = affected_requests * error_rate_delta * revenue_per_request
    return error_rate_delta, estimated


class BusinessImpactService:
    def _avg_metric(
        self, db: Session, service: str, metric: str, start: datetime, end: datetime
    ) -> Optional[float]:
        stmt = select(func.avg(Metric.value)).where(
            Metric.service_name == service,
            Metric.metric_name == metric,
            Metric.recorded_at >= start,
            Metric.recorded_at < end,
        )
        return db.execute(stmt).scalar()

    def _count_affected(
        self, db: Session, service: str, start: datetime, end: datetime
    ) -> int:
        # Proxy affected requests by ERROR-level log volume in-window (each error
        # ~ one impacted request). More defensible than metric-sample count; in a
        # real system this would be an actual request/error counter from telemetry.
        stmt = select(func.count(Log.id)).where(
            Log.service_name == service,
            Log.level == "ERROR",
            Log.logged_at >= start,
            Log.logged_at < end,
        )
        return int(db.execute(stmt).scalar() or 0)

    def calculate(self, db: Session, incident: Incident) -> BusinessImpact:
        # The signals that TRIGGER an incident precede its detection, so the
        # "incident window" is the lookback ending at detected_at, and the
        # baseline is the window immediately before that.
        window = settings.rca_lookback_minutes
        incident_end = incident.detected_at
        incident_start = incident.detected_at - timedelta(minutes=window)
        baseline_start = incident.detected_at - timedelta(minutes=2 * window)

        incident_err = self._avg_metric(
            db, incident.service_name, "error_rate", incident_start, incident_end
        ) or 0.0
        baseline_err = self._avg_metric(
            db, incident.service_name, "error_rate", baseline_start, incident_start
        ) or 0.0
        affected = self._count_affected(db, incident.service_name, incident_start, incident_end)

        rpr = settings.revenue_per_request
        error_rate_delta, estimated = compute_impact(affected, incident_err, baseline_err, rpr)

        basis = {
            "formula": "affected_requests * error_rate_delta * revenue_per_request",
            "affected_requests": affected,
            "affected_requests_proxy": "count of ERROR logs in incident window",
            "incident_error_rate": round(incident_err, 4),
            "baseline_error_rate": round(baseline_err, 4),
            "revenue_per_request": rpr,
            "window_minutes": window,
            "note": "revenue_per_request is a configurable mock; prod uses finance data",
        }

        row = BusinessImpact(
            incident_id=incident.id,
            affected_requests=affected,
            error_rate_delta=round(error_rate_delta, 4),
            estimated_impact_value=round(estimated, 2),
            calculation_basis=basis,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
