"""Incident, RCA, and business-impact endpoints."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models import BusinessImpact, Deployment, Incident, Log, Metric, RCAReport
from app.schemas import (
    BusinessImpactOut,
    IncidentDetail,
    IncidentOut,
    LogOut,
    MetricOut,
    RCAReportOut,
)
from app.services import business_impact_service, rca_engine

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=list[IncidentOut])
def list_incidents(db: Session = Depends(get_db)):
    stmt = select(Incident).order_by(Incident.detected_at.desc()).limit(100)
    return list(db.execute(stmt).scalars())


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: str, db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")

    since = incident.detected_at - timedelta(minutes=settings.rca_lookback_minutes)
    logs = list(
        db.execute(
            select(Log)
            .where(Log.service_name == incident.service_name, Log.logged_at >= since)
            .order_by(Log.logged_at.desc())
            .limit(50)
        ).scalars()
    )
    metrics = list(
        db.execute(
            select(Metric)
            .where(Metric.service_name == incident.service_name, Metric.recorded_at >= since)
            .order_by(Metric.recorded_at.desc())
            .limit(100)
        ).scalars()
    )
    timeline = _build_timeline(db, incident)

    detail = IncidentDetail.model_validate(incident)
    detail.logs = [LogOut.model_validate(l) for l in logs]
    detail.metrics = [MetricOut.model_validate(m) for m in metrics]
    detail.timeline = timeline
    return detail


def _build_timeline(db: Session, incident: Incident) -> list[dict]:
    events: list[dict] = []
    if incident.related_deployment_id:
        dep = db.get(Deployment, incident.related_deployment_id)
        if dep:
            events.append(
                {"at": dep.deployed_at.isoformat(), "event": f"Deployment {dep.version} started"}
            )
            if dep.switched_at:
                events.append({"at": dep.switched_at.isoformat(), "event": "Traffic switched"})
    events.append({"at": incident.detected_at.isoformat(), "event": incident.trigger_reason})
    if incident.resolved_at:
        events.append({"at": incident.resolved_at.isoformat(), "event": "Incident resolved"})
    return sorted(events, key=lambda e: e["at"])


@router.post("/{incident_id}/trigger-rca", response_model=RCAReportOut)
def trigger_rca(incident_id: str, db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")
    report = rca_engine.run(db, incident)
    return report


@router.get("/{incident_id}/rca-report", response_model=RCAReportOut)
def get_rca_report(incident_id: str, db: Session = Depends(get_db)):
    stmt = (
        select(RCAReport)
        .where(RCAReport.incident_id == incident_id)
        .order_by(RCAReport.generated_at.desc())
        .limit(1)
    )
    report = db.execute(stmt).scalars().first()
    if not report:
        raise HTTPException(404, "no RCA report yet — call trigger-rca first")
    return report


@router.get("/{incident_id}/business-impact", response_model=BusinessImpactOut)
def get_business_impact(incident_id: str, db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")
    existing = (
        db.execute(
            select(BusinessImpact)
            .where(BusinessImpact.incident_id == incident_id)
            .order_by(BusinessImpact.calculated_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing:
        return existing
    return business_impact_service.calculate(db, incident)
