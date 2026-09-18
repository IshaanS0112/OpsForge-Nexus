"""Deployment endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.db.session import get_db
from app.models import Deployment
from app.schemas import DeploymentCreate, DeploymentOut, K8sRolloutStatus, PromoteResult
from app.services.deployment_controller import DeploymentConflict, UnknownService
from app.services.deployment_engine import CANARY_WEIGHT, DeployState
from app.services.runtime import Runtime, get_runtime

router = APIRouter(prefix="/deployments", tags=["deployments"])


def _out(dep: Deployment) -> DeploymentOut:
    out = DeploymentOut.model_validate(dep)
    try:
        out.canary_weight = CANARY_WEIGHT.get(DeployState(dep.status), 0)
    except ValueError:
        out.canary_weight = 0
    return out


def _get(db: Session, deployment_id: str) -> Deployment:
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "deployment not found")
    return dep


@router.post("", response_model=DeploymentOut, status_code=202)
def create_deployment(
    payload: DeploymentCreate,
    db: Session = Depends(get_db),
    rt: Runtime = Depends(get_runtime),
):
    """Start a rollout. Returns 202 with the record at IDLE; the control loop
    then applies the version to the candidate colour's Deployment, polls the
    real rollout status, runs the health gate, and either walks the canary
    stages (5 -> 25 -> 50 -> 100%, each burn-rate gated) or, for BLUE_GREEN,
    switches the live Service selector. Poll GET /deployments/{id}."""
    try:
        dep = rt.deployments.create(
            db,
            service_name=payload.service_name,
            version=payload.version,
            now=utcnow(),
            strategy=payload.strategy,
            config_diff=payload.config_diff,
            image=payload.image,
            auto_promote=payload.auto_promote,
            simulate_failure=payload.simulate_failure,
            previous_version_id=payload.previous_version_id,
        )
    except UnknownService:
        raise HTTPException(404, f"unknown service {payload.service_name!r}; see GET /services") from None
    except DeploymentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    rt.wake()
    return _out(dep)


@router.get("", response_model=list[DeploymentOut])
def list_deployments(service: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Deployment).order_by(Deployment.deployed_at.desc()).limit(100)
    if service:
        stmt = stmt.where(Deployment.service_name == service)
    return [_out(d) for d in db.execute(stmt).scalars()]


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(deployment_id: str, db: Session = Depends(get_db)):
    return _out(_get(db, deployment_id))


@router.post("/{deployment_id}/rollback", response_model=DeploymentOut)
def manual_rollback(deployment_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Manual rollback bypasses cooldown (operator override): traffic returns
    to the stable colour and the candidate is scaled to zero."""
    dep = _get(db, deployment_id)
    try:
        rt.deployments.manual_rollback(db, dep, utcnow())
    except DeploymentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.refresh(dep)
    return _out(dep)


@router.post("/{deployment_id}/promote-canary", response_model=PromoteResult)
def promote_canary(deployment_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Advance to the next canary stage now. The gate is checked first: the
    canary needs enough traffic and no confident burn-rate regression. A
    failing gate rolls the canary back instead (409)."""
    dep = _get(db, deployment_id)
    try:
        decision = rt.deployments.promote(db, dep, utcnow())
    except DeploymentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.refresh(dep)
    result = PromoteResult(action=decision.action, verdict=decision.verdict.to_dict(), deployment=_out(dep))
    if decision.action == "rollback":
        raise HTTPException(409, detail=result.model_dump(mode="json"))
    if decision.action == "wait":
        raise HTTPException(409, detail=result.model_dump(mode="json"))
    return result


@router.get("/{deployment_id}/k8s-status", response_model=K8sRolloutStatus)
def k8s_status(deployment_id: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Live rollout status read from the cluster now: the candidate and stable
    colours' Deployments and pods, and the live Service's selector."""
    dep = _get(db, deployment_id)
    cluster = rt.cluster

    def colour(color: str | None):
        if not color:
            return None
        try:
            return cluster.rollout_status(dep.service_name, color).to_dict()
        except Exception as exc:  # noqa: BLE001 — report, don't 500
            return {"error": str(exc)}

    try:
        live = cluster.live_color(dep.service_name)
    except Exception:  # noqa: BLE001
        live = None
    return K8sRolloutStatus(
        deployment_id=dep.id,
        cluster_mode=cluster.mode,
        namespace=cluster.namespace,
        live_color=live,
        routing=rt.routing.route(dep.service_name).__dict__,
        candidate=colour(dep.target_color),
        stable=colour(dep.stable_color),
        gate=rt.deployments.last_verdict.get(dep.id),
    )
