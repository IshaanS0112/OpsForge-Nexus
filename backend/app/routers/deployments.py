"""Deployment endpoints."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Deployment
from app.schemas import DeploymentCreate, DeploymentOut
from app.services import rollback_engine
from app.services.deployment_engine import DeployState
from app.services.orchestrator import (
    _open_incident,
    create_deployment_record,
    execute_deployment,
)

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.post("", response_model=DeploymentOut, status_code=202)
def create_deployment(
    payload: DeploymentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Trigger a new blue-green deployment. Returns 202 immediately with the
    record at IDLE; the state machine runs the health gate over its real polling
    window in the background, then switches traffic or auto-rolls-back. Poll
    GET /deployments/{id} for the terminal state (LIVE or ROLLED_BACK)."""
    dep = create_deployment_record(db, payload)
    background_tasks.add_task(execute_deployment, dep.id, payload.simulate_failure)
    return dep


@router.get("", response_model=list[DeploymentOut])
def list_deployments(db: Session = Depends(get_db)):
    stmt = select(Deployment).order_by(Deployment.deployed_at.desc()).limit(100)
    return list(db.execute(stmt).scalars())


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(deployment_id: str, db: Session = Depends(get_db)):
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "deployment not found")
    return dep


@router.post("/{deployment_id}/rollback", response_model=DeploymentOut)
def manual_rollback(deployment_id: str, db: Session = Depends(get_db)):
    """Manual rollback bypasses cooldown (operator override)."""
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "deployment not found")
    if dep.status in (DeployState.ROLLED_BACK.value, DeployState.ROLLING_BACK.value):
        raise HTTPException(409, "deployment already rolling back / rolled back")

    decision = rollback_engine.evaluate(dep.service_name, manual=True)
    dep.status = DeployState.ROLLED_BACK.value
    incident = _open_incident(db, dep, decision.reason)
    rollback_engine.start_cooldown(dep.service_name)
    db.commit()
    db.refresh(dep)
    return dep
