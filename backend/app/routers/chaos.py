"""Chaos drill endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.db.session import get_db
from app.models import ChaosExperiment
from app.schemas import ChaosExperimentOut, ChaosInject
from app.services.chaos_engine import FAULT_TYPES, ChaosError
from app.services.runtime import Runtime, get_runtime

router = APIRouter(prefix="/chaos", tags=["chaos"])


@router.get("/fault-types")
def fault_types():
    return FAULT_TYPES


@router.post("/inject", response_model=ChaosExperimentOut, status_code=201)
def inject(payload: ChaosInject, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Inject a real fault and start measuring: detection latency (to the
    first tick where >= 2 anomaly layers agree), rollback latency (for a
    canary-targeted drill) and recovery latency."""
    try:
        exp = rt.chaos.inject(
            db,
            service_name=payload.service_name,
            fault_type=payload.fault_type,
            now=utcnow(),
            magnitude=payload.magnitude,
            target=payload.target,
            duration_s=payload.duration_s,
            detection_bound_s=payload.detection_bound_s,
            rollback_bound_s=payload.rollback_bound_s,
        )
    except ChaosError as exc:
        raise HTTPException(409 if "already running" in str(exc) else 422, str(exc)) from exc
    return exp


@router.get("/experiments", response_model=list[ChaosExperimentOut])
def list_experiments(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    return rt.chaos.recent(db)


@router.get("/experiments/{experiment_id}", response_model=ChaosExperimentOut)
def get_experiment(experiment_id: str, db: Session = Depends(get_db)):
    exp = db.get(ChaosExperiment, experiment_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    return exp


@router.post("/experiments/{experiment_id}/abort", response_model=ChaosExperimentOut)
def abort_experiment(experiment_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    exp = db.get(ChaosExperiment, experiment_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    try:
        return rt.chaos.abort(db, exp, utcnow())
    except ChaosError as exc:
        raise HTTPException(409, str(exc)) from exc
