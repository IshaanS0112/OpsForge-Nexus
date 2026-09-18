"""Service, dependency-graph, SLO and anomaly-consensus endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog
from app.clock import utcnow
from app.db.session import get_db
from app.models import AnomalyConsensus, BurnRateAlert, ServiceDependency, SLOConfig
from app.schemas import EdgeCreate, SLOUpdate
from app.services.dependency_graph import CycleError
from app.services.runtime import Runtime, get_runtime

router = APIRouter(prefix="/services", tags=["services"])


def _known(rt: Runtime, db: Session, name: str) -> None:
    if name not in rt.graph(db) and catalog.get(name) is None:
        raise HTTPException(404, f"unknown service {name!r}")


@router.get("")
def list_services(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    graph = rt.graph(db)
    layers = graph.layers()
    out = []
    for name in rt.service_order(db):
        spec = catalog.get(name)
        r, b = rt.results.get(name), rt.burns.get(name)
        out.append(
            {
                "name": name,
                "tier": spec.tier if spec else None,
                "layer": layers.get(name, 0),
                "status": rt.health_status(name),
                "upstream": graph.upstream(name),
                "downstream": graph.downstream(name),
                "route": rt.routing.route(name).__dict__,
                "values": rt.values.get(name),
                "traffic": rt.traffic.get(name),
                "agreeing_layers": r.agreeing_layers if r else 0,
                "burn_worst": b.worst if b else None,
            }
        )
    return out


@router.get("/graph")
def dependency_graph(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    return rt.graph(db).to_dict()


@router.post("/graph/edges", status_code=201)
def add_edge(payload: EdgeCreate, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Declare that ``service_name`` depends on ``depends_on_service``. Rejected
    if it would create a cycle: the graph must stay a DAG."""
    graph = rt.graph(db)
    try:
        graph.with_edge(payload.service_name, payload.depends_on_service)
    except CycleError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (payload.service_name, payload.depends_on_service) in graph.edges():
        raise HTTPException(409, "edge already exists")
    db.add(ServiceDependency(service_name=payload.service_name, depends_on_service=payload.depends_on_service))
    db.commit()
    rt.invalidate_graph()
    return rt.graph(db).to_dict()


@router.delete("/graph/edges")
def delete_edge(
    service_name: str, depends_on_service: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)
):
    row = (
        db.execute(
            select(ServiceDependency).where(
                ServiceDependency.service_name == service_name,
                ServiceDependency.depends_on_service == depends_on_service,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(404, "edge not found")
    db.delete(row)
    db.commit()
    rt.invalidate_graph()
    return rt.graph(db).to_dict()


@router.get("/{name}/dependencies")
def dependencies(name: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Upstream (what it calls) and downstream (what calls it) edges."""
    _known(rt, db, name)
    g = rt.graph(db)
    return {
        "service": name,
        "upstream": g.upstream(name),
        "downstream": g.downstream(name),
        "upstream_closure": g.upstream_closure(name),
    }


@router.get("/{name}/blast-radius")
def blast_radius(name: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Every service reachable through dependents (BFS, nearest first): what
    can break when ``name`` does."""
    _known(rt, db, name)
    services = rt.graph(db).blast_radius(name)
    return {"service": name, "blast_radius": services, "size": len(services)}


@router.get("/{name}/slo-burn-rate")
def slo_burn_rate(name: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Burn rate across all three multiwindow alerts, computed now, plus
    recent alert history."""
    _known(rt, db, name)
    burn = rt.burn.evaluate(db, name, utcnow())
    alerts = db.execute(
        select(BurnRateAlert)
        .where(BurnRateAlert.service_name == name)
        .order_by(BurnRateAlert.fired_at.desc())
        .limit(20)
    ).scalars()
    return {
        **burn.to_dict(),
        "alerts": [
            {
                "severity": a.severity,
                "long_window": a.long_window,
                "short_window": a.short_window,
                "burn_rate": a.burn_rate,
                "short_burn_rate": a.short_burn_rate,
                "fired_at": a.fired_at,
                "resolved_at": a.resolved_at,
            }
            for a in alerts
        ],
    }


@router.put("/{name}/slo")
def update_slo(name: str, payload: SLOUpdate, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    _known(rt, db, name)
    cfg = db.get(SLOConfig, name) or rt.burn.slo(db, name)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    db.merge(cfg)
    db.commit()
    rt.recorder.set_latency_bound(name, cfg.latency_threshold_ms)
    return {
        "service_name": name,
        "slo_target": cfg.slo_target,
        "window_days": cfg.window_days,
        "latency_threshold_ms": cfg.latency_threshold_ms,
    }


@router.get("/{name}/anomaly-consensus")
def anomaly_consensus(name: str, db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Latest per-layer flags and agreement count (this tick), plus the most
    recent recorded consensus rows."""
    _known(rt, db, name)
    r = rt.results.get(name)
    rows = db.execute(
        select(AnomalyConsensus)
        .where(AnomalyConsensus.service_name == name)
        .order_by(AnomalyConsensus.window_end.desc())
        .limit(30)
    ).scalars()
    return {
        "service": name,
        "latest": {**r.summary(), "window_end": r.at} if r else None,
        "history": [
            {
                "window_end": c.window_end,
                "zscore_flagged": c.zscore_flagged,
                "seasonal_flagged": c.seasonal_flagged,
                "isolation_forest_flagged": c.isolation_forest_flagged,
                "agreeing_layers": c.agreeing_layers,
                "auto_action_taken": c.auto_action_taken,
            }
            for c in rows
        ],
    }
