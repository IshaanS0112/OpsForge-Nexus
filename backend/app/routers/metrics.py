"""Metric ingestion + query.

The control loop measures the catalog services itself. These endpoints are
for external telemetry (``scripts/metric_generator.py``):

* ``/metrics/ingest-batch`` takes one reading of all four metrics and runs it
  through the full three-layer suite and consensus gate — the path that can
  open an incident.
* ``/metrics/ingest`` (V1) stores a single metric point with its z-score. One
  metric can only ever feed one layer, and a single layer never auto-acts, so
  this path no longer opens incidents; it reports the z-score streak instead.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.db.session import get_db
from app.models import Metric
from app.schemas import MetricBatchIngest, MetricIngest, MetricOut
from app.services import anomaly_service
from app.services.runtime import Runtime, get_runtime

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.post("/ingest", response_model=MetricOut, status_code=201)
def ingest_metric(payload: MetricIngest, db: Session = Depends(get_db)):
    """Ingest one metric point and compute its z-score against the rolling
    window (V1 path; see module docstring)."""
    row, _ = anomaly_service.ingest(db, payload.service_name, payload.metric_name, payload.value)
    return row


@router.post("/ingest-batch", status_code=201)
def ingest_batch(payload: MetricBatchIngest, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Evaluate one four-metric reading with all three detector layers; two
    agreeing layers open an incident, exactly as for measured traffic."""
    values = {
        "error_rate": payload.error_rate,
        "latency_p95": payload.latency_p95,
        "cpu_usage": payload.cpu_usage,
        "memory_usage": payload.memory_usage,
    }
    errors = round(payload.error_rate * payload.requests)
    result = rt.ingest_external(db, payload.service_name, values, errors, payload.requests, utcnow())
    return {
        "service_name": payload.service_name,
        **result.summary(),
        "zscores": {k: v for k, v in result.zscores.items() if v is not None},
        "residual_z": {k: v for k, v in result.residual_z.items() if v is not None},
        "isolation_forest_score": result.iforest_score,
    }


@router.get("/{service_name}", response_model=list[MetricOut])
def query_metrics(service_name: str, window: str = "15m", db: Session = Depends(get_db)):
    minutes = _parse_window(window)
    since = utcnow() - timedelta(minutes=minutes)
    stmt = (
        select(Metric)
        .where(Metric.service_name == service_name, Metric.recorded_at >= since)
        .order_by(Metric.recorded_at.desc())
        .limit(2000)
    )
    return list(db.execute(stmt).scalars())


def _parse_window(window: str) -> int:
    window = window.strip().lower()
    if window.endswith("m"):
        return int(window[:-1] or settings.rca_lookback_minutes)
    if window.endswith("h"):
        return int(window[:-1] or 1) * 60
    return int(window or settings.rca_lookback_minutes)
