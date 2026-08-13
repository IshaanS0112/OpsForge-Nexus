"""Metric ingestion + query. The simulated generator POSTs to /metrics/ingest."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Metric
from app.schemas import MetricIngest, MetricOut
from app.services import anomaly_service
from app.services.orchestrator import create_incident_from_anomaly

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.post("/ingest", response_model=MetricOut, status_code=201)
def ingest_metric(payload: MetricIngest, db: Session = Depends(get_db)):
    """Ingest one metric point. Computes its z-score against the rolling window
    and, if K consecutive anomalies are seen, auto-opens an incident."""
    row, should_incident = anomaly_service.ingest(
        db, payload.service_name, payload.metric_name, payload.value
    )
    if should_incident:
        create_incident_from_anomaly(
            db, payload.service_name, payload.metric_name, row.z_score or 0.0
        )
        anomaly_service.streak.reset(payload.service_name, payload.metric_name)
    return row


@router.get("/{service_name}", response_model=list[MetricOut])
def query_metrics(service_name: str, window: str = "15m", db: Session = Depends(get_db)):
    minutes = _parse_window(window)
    since = datetime.utcnow() - timedelta(minutes=minutes)
    stmt = (
        select(Metric)
        .where(Metric.service_name == service_name, Metric.recorded_at >= since)
        .order_by(Metric.recorded_at.desc())
        .limit(500)
    )
    return list(db.execute(stmt).scalars())


def _parse_window(window: str) -> int:
    window = window.strip().lower()
    if window.endswith("m"):
        return int(window[:-1] or 15)
    if window.endswith("h"):
        return int(window[:-1] or 1) * 60
    return int(window or 15)
