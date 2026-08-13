"""Deployment orchestration: drives the state machine through the health gate
and hands off to the rollback engine + incident creation on failure.

Kept separate from the pure state machine so the machine stays trivially
testable while this layer owns persistence and side effects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db import session as db_session
from app.models import Deployment, Incident, Log, Metric
from app.schemas import DeploymentCreate
from app.services import rollback_engine
from app.services.deployment_engine import (
    DeploymentStateMachine,
    DeployState,
    HealthChecker,
    make_sampler,
)

logger = logging.getLogger(__name__)


def _persist(db: Session, dep: Deployment, state: DeployState) -> None:
    dep.status = state.value
    db.commit()


def create_deployment_record(db: Session, payload: DeploymentCreate) -> Deployment:
    """Persist a new deployment at IDLE and return immediately. The actual
    state-machine run + health gate happen asynchronously in
    ``execute_deployment`` so the request never blocks on the polling window."""
    dep = Deployment(
        service_name=payload.service_name,
        version=payload.version,
        status=DeployState.IDLE.value,
        config_diff=payload.config_diff,
        previous_version_id=payload.previous_version_id,
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


def execute_deployment(deployment_id: str, simulate_failure: bool) -> None:
    """Background worker: drives the state machine through the *real* time-based
    health gate, then switches traffic or rolls back. Runs with its own DB
    session because it executes outside the request lifecycle."""
    db = db_session.SessionLocal()
    try:
        dep = db.get(Deployment, deployment_id)
        if dep is None:
            logger.warning("execute_deployment: %s not found", deployment_id)
            return

        sm = DeploymentStateMachine(DeployState.IDLE)
        for state in (DeployState.PREPARING_GREEN, DeployState.DEPLOYING_GREEN, DeployState.HEALTH_CHECKING):
            sm.transition(state)
            _persist(db, dep, state)

        # --- health gate: genuinely polls over time (see HealthChecker.run) ---
        result = HealthChecker().run(make_sampler(simulate_failure))

        if result.passed:
            sm.transition(DeployState.TRAFFIC_SWITCHING)
            _persist(db, dep, DeployState.TRAFFIC_SWITCHING)
            sm.transition(DeployState.LIVE)
            dep.switched_at = datetime.utcnow()
            _persist(db, dep, DeployState.LIVE)
            logger.info("Deployment %s LIVE (%s %s)", dep.id, dep.service_name, dep.version)
            return

        # --- health gate failed: ask the rollback engine ---
        decision = rollback_engine.evaluate(
            dep.service_name,
            consecutive_health_failures=result.consecutive_failures,
        )
        if not decision.should_rollback:
            logger.info("Health failed but rollback not fired for %s: %s", dep.service_name, decision.reason)
            return

        sm.transition(DeployState.ROLLING_BACK)
        _persist(db, dep, DeployState.ROLLING_BACK)

        incident = _open_incident(db, dep, decision.reason)
        _seed_failure_signals(db, dep, incident)

        sm.transition(DeployState.ROLLED_BACK)
        _persist(db, dep, DeployState.ROLLED_BACK)
        rollback_engine.start_cooldown(dep.service_name)  # anti-flapping
        logger.info("Deployment %s ROLLED_BACK; incident %s opened", dep.id, incident.id)
    finally:
        db.close()


def _open_incident(db: Session, dep: Deployment, reason: str) -> Incident:
    incident = Incident(
        service_name=dep.service_name,
        status="OPEN",
        severity="HIGH",
        trigger_reason=reason,
        related_deployment_id=dep.id,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident


def create_incident_from_anomaly(db: Session, service: str, metric: str, z: float) -> Incident:
    """Called by the metrics path when the z-score streak crosses K."""
    incident = Incident(
        service_name=service,
        status="OPEN",
        severity="HIGH" if abs(z) > 4 else "MEDIUM",
        trigger_reason=f"{metric} z-score {z:.2f} exceeded threshold for K consecutive readings",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident


def _seed_failure_signals(db: Session, dep: Deployment, incident: Incident) -> None:
    """Insert representative anomalous metrics + error logs so the RCA pipeline
    has real, in-window signals to collect for the demo. Clearly simulated."""
    now = incident.detected_at
    seeds = [
        ("error_rate", 0.12, 4.2),
        ("latency_p95", 1200.0, 3.8),
    ]
    for name, value, z in seeds:
        db.add(
            Metric(
                service_name=dep.service_name,
                metric_name=name,
                value=value,
                z_score=z,
                recorded_at=now - timedelta(seconds=30),
            )
        )
    error_logs = [
        ("NullPointerException:UserService.getProfile", 342),
        ("TimeoutException:DBConnectionPool", 89),
    ]
    for sig, count in error_logs:
        # One row per occurrence so the RCA signal counter reflects real counts.
        for _ in range(count):
            db.add(
                Log(
                    service_name=dep.service_name,
                    level="ERROR",
                    message=f"{sig} (simulated)",
                    error_signature=sig,
                    incident_id=incident.id,
                    logged_at=now - timedelta(seconds=20),
                )
            )
    db.commit()
