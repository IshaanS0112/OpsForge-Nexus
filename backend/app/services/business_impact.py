"""Business impact calculator.

Formula (assumptions are explicit and stored with every result):

    affected_requests   = COUNT(requests during incident window)
    error_rate_delta    = incident_period_error_rate - baseline_error_rate
    estimated_impact    = affected_requests * error_rate_delta * revenue_per_request

i.e. the requests that failed *beyond what normally fails*, priced per
request. ``revenue_per_request`` is a CONFIGURABLE business parameter (mock
default $0.50); in production it would come from finance/analytics' real
conversion data. Every input is stored in ``calculation_basis`` so the number
is auditable — "here is the formula and the inputs", not a black-box figure.

V3 changes:

* Measured counts. The gateway counts every request and every bad one per
  service (``sli_samples``), so ``affected_requests`` is the real request
  volume in the window. V1 had no request counter and used the ERROR-log
  count instead — which already counts failures, so multiplying it by the
  error-rate delta applied the rate twice. The log proxy remains only as a
  fallback for services without measured traffic.
* Blast radius. For the root incident of a cascade, the sum runs over the
  root service and every service whose incident cascaded from it; each
  service's requests are distinct edge requests, so nothing is counted twice.
  A cascading child's own impact is reported against the root.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.models import BusinessImpact, Incident, Log, Metric, SLISample

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
    # Detection lags onset by the detectors' K consecutive readings; start the
    # impact window a little before detection to include the onset.
    ONSET_LEAD = timedelta(minutes=2)

    def _sli(self, db: Session, service: str, start: datetime, end: datetime) -> tuple[int, int]:
        row = db.execute(
            select(
                func.coalesce(func.sum(SLISample.total_requests), 0),
                func.coalesce(func.sum(SLISample.bad_requests), 0),
            ).where(
                SLISample.service_name == service,
                SLISample.window_end > start,
                SLISample.window_end <= end,
            )
        ).one()
        return int(row[0]), int(row[1])

    def _avg_metric(self, db: Session, service: str, metric: str, start: datetime, end: datetime) -> Optional[float]:
        stmt = select(func.avg(Metric.value)).where(
            Metric.service_name == service,
            Metric.metric_name == metric,
            Metric.recorded_at >= start,
            Metric.recorded_at < end,
        )
        return db.execute(stmt).scalar()

    def _error_logs(self, db: Session, service: str, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.sum(Log.occurrences), 0)).where(
            Log.service_name == service,
            Log.level == "ERROR",
            Log.logged_at >= start,
            Log.logged_at < end,
        )
        return int(db.execute(stmt).scalar() or 0)

    def _service_impact(self, db: Session, service: str, start: datetime, end: datetime, rpr: float) -> dict:
        window = end - start
        baseline_start = start - max(window, timedelta(minutes=settings.rca_lookback_minutes))
        total, bad = self._sli(db, service, start, end)
        base_total, base_bad = self._sli(db, service, baseline_start, start)
        if total > 0:
            rate = bad / total
            base_rate = base_bad / base_total if base_total else 0.0
            delta, impact = compute_impact(total, rate, base_rate, rpr)
            return {
                "service": service,
                "source": "measured",
                "affected_requests": total,
                "bad_requests": bad,
                "incident_bad_ratio": round(rate, 5),
                "baseline_bad_ratio": round(base_rate, 5),
                "error_rate_delta": round(delta, 5),
                "estimated_impact": round(impact, 2),
            }
        # No measured traffic: the V1 method (metric averages, ERROR logs as the request proxy).
        rate = self._avg_metric(db, service, "error_rate", start, end) or 0.0
        base_rate = self._avg_metric(db, service, "error_rate", baseline_start, start) or 0.0
        affected = self._error_logs(db, service, start, end)
        delta, impact = compute_impact(affected, rate, base_rate, rpr)
        return {
            "service": service,
            "source": "error-log proxy (no measured traffic)",
            "affected_requests": affected,
            "incident_bad_ratio": round(rate, 5),
            "baseline_bad_ratio": round(base_rate, 5),
            "error_rate_delta": round(delta, 5),
            "estimated_impact": round(impact, 2),
        }

    def calculate(self, db: Session, incident: Incident) -> BusinessImpact:
        rpr = settings.revenue_per_request
        start = incident.detected_at - self.ONSET_LEAD
        end = incident.resolved_at or utcnow()
        if end <= start:
            end = incident.detected_at + timedelta(seconds=1)

        services = [incident.service_name]
        if not incident.is_cascading:
            children = db.execute(select(Incident).where(Incident.root_incident_id == incident.id)).scalars()
            services += [c.service_name for c in children if c.service_name not in services]
        per_service = [self._service_impact(db, s, start, end, rpr) for s in services]

        affected = sum(p["affected_requests"] for p in per_service)
        estimated = sum(p["estimated_impact"] for p in per_service)
        root = per_service[0]
        basis = {
            "formula": "affected_requests * error_rate_delta * revenue_per_request, summed per service",
            "affected_requests": affected,
            "affected_requests_source": root["source"],
            "incident_error_rate": root["incident_bad_ratio"],
            "baseline_error_rate": root["baseline_bad_ratio"],
            "revenue_per_request": rpr,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "open": incident.resolved_at is None},
            "scope": "cascade (root + cascading children)" if len(per_service) > 1 else "single service",
            "per_service": per_service,
            "note": "revenue_per_request is a configurable mock; prod uses finance data",
        }
        if incident.is_cascading:
            basis["note"] += "; this incident cascades from its root, whose impact includes it"

        row = BusinessImpact(
            incident_id=incident.id,
            affected_requests=affected,
            error_rate_delta=root["error_rate_delta"],
            estimated_impact_value=round(estimated, 2),
            calculation_basis=basis,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
