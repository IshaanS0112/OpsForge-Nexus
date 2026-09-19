"""Cluster, gateway and dashboard-snapshot endpoints."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog
from app.clock import utcnow
from app.config import settings
from app.db.session import get_db
from app.models import ChaosExperiment, Deployment
from app.services.burn_rate_engine import SEVERITY_RANK
from app.services.deployment_engine import ACTIVE_STATES, CANARY_WEIGHT, DeployState
from app.services.runtime import Runtime, get_runtime

router = APIRouter(tags=["console"])


@router.get("/cluster")
def cluster_info(rt: Runtime = Depends(get_runtime)):
    """Which cluster the backend drives (``kind`` = real API calls, or
    ``simulated``), whether it is reachable, and the control loop's health."""
    return {
        **rt.cluster.info(),
        "bootstrapped": rt.bootstrapped,
        "bootstrap_error": rt.cluster_error,
        "control_loop": {
            "running": rt.running,
            "tick_s": rt.tick_s,
            "ticks": rt.tick_count,
            "last_tick_at": rt.last_tick_at,
            "last_tick_ms": rt.last_tick_ms,
        },
        "load_generator": {
            "running": bool(rt.loadgen and rt.loadgen.running),
            "serving": rt.serving,  # every live colour Ready, so traffic is flowing
            "rps_per_service": settings.loadgen_rps_per_service,
            "cycle_period_s": settings.loadgen_cycle_period_s,
        },
    }


@router.get("/gateway/routes")
def gateway_routes(rt: Runtime = Depends(get_runtime)):
    return rt.routing.snapshot()


@router.get("/gateway/{service}")
async def gateway_request(service: str, rt: Runtime = Depends(get_runtime)):
    """Send one request through the gateway, exactly as the load generator
    does (weighted canary routing included), and show what happened."""
    if catalog.get(service) is None:
        raise HTTPException(404, f"unknown service {service!r}")
    outcome = await rt.gateway.send(service)
    return outcome.__dict__


def _live_versions(db: Session) -> dict[str, str]:
    out = {s.name: s.initial_version for s in catalog.CATALOG}
    rows = db.execute(
        select(Deployment).where(Deployment.status == DeployState.LIVE.value).order_by(Deployment.switched_at)
    ).scalars()
    for d in rows:
        out[d.service_name] = d.version
    return out


@router.get("/console/snapshot")
def console_snapshot(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    """Everything the Ops Console renders, in one call."""
    now = utcnow()
    graph = rt.graph(db)
    layers = graph.layers()
    active_incidents = {i.service_name: i for i in rt.incidents.all_active(db)}
    active_deps = {
        d.service_name: d
        for d in db.execute(select(Deployment).where(Deployment.status.in_([s.value for s in ACTIVE_STATES]))).scalars()
    }
    recent_rollbacks = {
        d.service_name
        for d in db.execute(
            select(Deployment).where(
                Deployment.status == DeployState.ROLLED_BACK.value,
                Deployment.finished_at >= now - timedelta(seconds=60),
            )
        ).scalars()
    }
    running_drills = list(db.execute(select(ChaosExperiment).where(ChaosExperiment.status == "RUNNING")).scalars())
    versions = _live_versions(db)

    services = []
    worst = None  # most severe firing alert
    max_burn = None  # highest current 1h burn rate, firing or not
    for name in rt.service_order(db):
        spec = catalog.get(name)
        r, b = rt.results.get(name), rt.burns.get(name)
        fast = next((e for e in b.evaluations if e.severity == "FAST_BURN"), None) if b else None
        inc = active_incidents.get(name)
        dep = active_deps.get(name)
        status = rt.health_status(name, recently_rolled_back=name in recent_rollbacks)
        burn_1h = round(fast.long_burn, 2) if fast and fast.long_burn is not None else None
        if burn_1h is not None and (max_burn is None or burn_1h > max_burn["burn"]):
            max_burn = {"service": name, "burn": burn_1h}
        if b and b.worst:
            rank = (SEVERITY_RANK[b.worst], -(burn_1h or 0.0))
            if worst is None or rank < (SEVERITY_RANK[worst["severity"]], -worst["burn"]):
                worst = {"service": name, "severity": b.worst, "burn": burn_1h or 0.0}
        services.append(
            {
                "name": name,
                "tier": spec.tier if spec else None,
                "layer": layers.get(name, 0),
                "status": status,
                "upstream": graph.upstream(name),
                "downstream": graph.downstream(name),
                "live_version": versions.get(name),
                "route": rt.routing.route(name).__dict__,
                "values": rt.values.get(name),
                "traffic": rt.traffic.get(name),
                "consensus": r.summary() if r else None,
                "burn": {
                    "worst": b.worst,
                    "fast_long": fast.long_burn if fast else None,
                    "fast_short": fast.short_burn if fast else None,
                    "budget_remaining": b.budget_remaining,
                    "slo_target": b.slo_target,
                    "latency_threshold_ms": b.latency_threshold_ms,
                }
                if b
                else None,
                "incident": {
                    "id": inc.id,
                    "status": inc.status,
                    "severity": inc.severity,
                    "is_cascading": bool(inc.is_cascading),
                    "root_incident_id": inc.root_incident_id,
                }
                if inc
                else None,
                "deployment": {
                    "id": dep.id,
                    "version": dep.version,
                    "status": dep.status,
                    "strategy": dep.strategy,
                    "weight": CANARY_WEIGHT.get(DeployState(dep.status), 0),
                    "target_color": dep.target_color,
                    "status_reason": dep.status_reason,
                    "gate": rt.deployments.last_verdict.get(dep.id),
                }
                if dep
                else None,
            }
        )

    return {
        "generated_at": now,
        "cluster": {
            "mode": rt.cluster.mode,
            "namespace": rt.cluster.namespace,
            "bootstrapped": rt.bootstrapped,
            "error": rt.cluster_error,
        },
        "loop": {
            "ticks": rt.tick_count,
            "last_tick_at": rt.last_tick_at,
            "last_tick_ms": rt.last_tick_ms,
            "tick_s": rt.tick_s,
        },
        "summary": {
            "services": len(services),
            "healthy": sum(1 for s in services if s["status"] == "ok"),
            "open_incidents": len(active_incidents),
            "worst_burn": worst,
            "max_burn_1h": max_burn,
            "active_rollouts": len(active_deps),
            "running_drills": len(running_drills),
        },
        "services": services,
        "edges": [{"service": s, "depends_on": d} for s, d in graph.edges()],
        "drills": [
            {
                "id": e.id,
                "service_name": e.service_name,
                "fault_type": e.fault_type,
                "target": e.target,
                "injected_at": e.injected_at,
                "detection_latency_seconds": e.detection_latency_seconds,
                "rollback_latency_seconds": e.rollback_latency_seconds,
            }
            for e in running_drills
        ],
    }
