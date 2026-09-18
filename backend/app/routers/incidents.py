"""Incident, cascade, RCA, and business-impact endpoints."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.db.session import get_db
from app.models import BusinessImpact, ChaosExperiment, Deployment, Incident, Log, Metric, RCAReport
from app.schemas import (
    BusinessImpactOut,
    CascadeTree,
    IncidentDetail,
    IncidentOut,
    LogOut,
    MetricOut,
    RCAReportOut,
)
from app.services import business_impact_service, rca_engine
from app.services.runtime import Runtime, get_runtime

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _get(db: Session, incident_id: str) -> Incident:
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")
    return incident


@router.get("", response_model=list[IncidentOut])
def list_incidents(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Incident).order_by(Incident.detected_at.desc()).limit(100)
    if status:
        stmt = stmt.where(Incident.status == status.upper())
    return list(db.execute(stmt).scalars())


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: str, db: Session = Depends(get_db)):
    incident = _get(db, incident_id)
    since = incident.detected_at - timedelta(minutes=settings.rca_lookback_minutes)
    until = incident.resolved_at or utcnow()
    logs = list(
        db.execute(
            select(Log)
            .where(Log.service_name == incident.service_name, Log.logged_at >= since, Log.logged_at <= until)
            .order_by(Log.logged_at.desc())
            .limit(50)
        ).scalars()
    )
    metrics = list(
        db.execute(
            select(Metric)
            .where(
                Metric.service_name == incident.service_name, Metric.recorded_at >= since, Metric.recorded_at <= until
            )
            .order_by(Metric.recorded_at.desc())
            .limit(400)
        ).scalars()
    )
    detail = IncidentDetail.model_validate(incident)
    detail.logs = [LogOut.model_validate(log) for log in logs]
    detail.metrics = [MetricOut.model_validate(m) for m in metrics]
    detail.timeline = _build_timeline(db, incident)
    return detail


def _build_timeline(db: Session, incident: Incident) -> list[dict]:
    events: list[dict] = []
    if incident.related_deployment_id:
        dep = db.get(Deployment, incident.related_deployment_id)
        if dep:
            events.append(
                {"at": dep.deployed_at.isoformat(), "event": f"Deployment {dep.version} started ({dep.strategy})"}
            )
            for stage in dep.stage_history or []:
                if stage.get("stage") in ("IDLE", "PREPARING_GREEN"):
                    continue
                events.append({"at": stage["at"], "event": f"{stage['stage']}: {stage.get('reason', '')}".strip(": ")})
    if incident.chaos_experiment_id:
        exp = db.get(ChaosExperiment, incident.chaos_experiment_id)
        if exp:
            events.append(
                {
                    "at": exp.injected_at.isoformat(),
                    "event": f"Chaos drill injected {exp.fault_type} on {exp.service_name}",
                }
            )
    events.append({"at": incident.detected_at.isoformat(), "event": f"Incident opened: {incident.trigger_reason}"})
    for e in (incident.detection_context or {}).get("events", []):
        events.append({"at": e["at"], "event": e["reason"]})
    if incident.resolved_at:
        events.append({"at": incident.resolved_at.isoformat(), "event": "Incident resolved"})
    return sorted(events, key=lambda e: e["at"])


@router.get("/{incident_id}/cascade-tree", response_model=CascadeTree)
def cascade_tree(incident_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """The root incident plus every incident attributed to it. ``edges`` link
    each child to its nearest affected upstream along the dependency graph."""
    _get(db, incident_id)
    tree = rt.incidents.cascade_tree(db, incident_id)
    graph = rt.graph(db)
    members = {tree["root"].service_name, *(c.service_name for c in tree["children"])}
    edges = []
    for child in tree["children"]:
        parents = [u for u in graph.upstream(child.service_name) if u in members]
        if not parents:  # connected through an unaffected hop: attach to the nearest affected upstream
            parents = [u for u in graph.upstream_closure(child.service_name) if u in members][:1]
        edges += [{"parent": p, "child": child.service_name} for p in parents]
    return CascadeTree(root=tree["root"], children=tree["children"], edges=edges)


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
def resolve_incident(incident_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    incident = _get(db, incident_id)
    if incident.status == "RESOLVED":
        raise HTTPException(409, "incident already resolved")
    rt.incidents.resolve(db, incident, utcnow(), "resolved by operator")
    db.commit()
    db.refresh(incident)
    return incident


@router.post("/{incident_id}/trigger-rca", response_model=RCAReportOut)
def trigger_rca(incident_id: str, db: Session = Depends(get_db)):
    incident = _get(db, incident_id)
    return rca_engine.run(db, incident)


@router.get("/{incident_id}/rca-report", response_model=RCAReportOut)
def get_rca_report(incident_id: str, db: Session = Depends(get_db)):
    stmt = (
        select(RCAReport).where(RCAReport.incident_id == incident_id).order_by(RCAReport.generated_at.desc()).limit(1)
    )
    report = db.execute(stmt).scalars().first()
    if not report:
        raise HTTPException(404, "no RCA report yet — call trigger-rca first")
    return report


@router.get("/{incident_id}/business-impact", response_model=BusinessImpactOut)
def get_business_impact(incident_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    incident = _get(db, incident_id)
    if not refresh:
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
