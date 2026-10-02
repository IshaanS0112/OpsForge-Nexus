"""Deployment controller: drives every in-flight rollout one step per
control-loop tick.

Controller-style reconciliation: the desired state lives in the
``deployments`` table and each tick moves a rollout forward from wherever it
is. A backend restart resumes rollouts mid-flight instead of abandoning them
(V1 ran each rollout in a background task that died with the process).

Per state:

    IDLE              apply the new version to the candidate colour's
                      Deployment (server-side apply)       -> DEPLOYING_GREEN
    DEPLOYING_GREEN   poll the real rollout status; fail on the progress
                      deadline or a fatal pod state (ImagePullBackOff,
                      CrashLoopBackOff...) held for rollout_fast_fail_s
                                                           -> HEALTH_CHECKING
    HEALTH_CHECKING   requests straight to the candidate colour. Blue-green:
                      the full health gate (one poll per tick over the window,
                      3 consecutive failed polls roll back) — nothing else
                      stands between it and 100% of traffic. Canary: one smoke
                      poll (is it serving at all?); the stages' burn-rate
                      gates judge its quality under real traffic.
                      -> CANARY_5 (canary) or TRAFFIC_SWITCHING (blue-green)
    CANARY_n          canary burn-rate gate (canary_engine)
    TRAFFIC_SWITCHING PATCH the live Service selector      -> LIVE
    ROLLING_BACK      traffic back to stable, selector restored if it had
                      moved, candidate scaled to zero      -> ROLLED_BACK
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog, observability
from app.config import settings
from app.k8s import manifests as m
from app.models import Deployment
from app.services.burn_rate_engine import burn_rate
from app.services.canary_engine import CanaryEngine, StageDecision
from app.services.dependency_graph import DependencyGraph
from app.services.deployment_engine import (
    ACTIVE_STATES,
    CANARY_STATES,
    CANARY_WEIGHT,
    DeploymentStateMachine,
    DeployState,
    HealthChecker,
    HealthGate,
    Strategy,
    sample_from_outcomes,
)
from app.services.gateway import Gateway, RoutingTable
from app.services.incident_service import IncidentService
from app.services.rollback_engine import RollbackDecision, RollbackEngine, RollbackTrigger

logger = logging.getLogger(__name__)

# simulate_failure ships the candidate with a bad config: this share of its
# requests return 500, and detection only ever sees the resulting traffic.
# 15% is subtle enough to pass the canary smoke check and reach the gates.
SIMULATED_FAILURE_ERROR_RATE = 0.15


class UnknownService(LookupError):
    pass


class DeploymentConflict(RuntimeError):
    pass


class DeploymentController:
    def __init__(
        self,
        cluster,
        gateway: Gateway,
        routing: RoutingTable,
        canary: CanaryEngine,
        rollback: RollbackEngine,
        incidents: IncidentService,
        graph: Callable[[Session], DependencyGraph],
    ) -> None:
        self.cluster = cluster
        self.gateway = gateway
        self.routing = routing
        self.canary = canary
        self.rollback_engine = rollback
        self.incidents = incidents
        self.graph = graph
        self.lock = threading.RLock()
        self._health: dict[str, HealthGate] = {}
        self._fatal_since: dict[str, datetime] = {}
        self.last_verdict: dict[str, dict] = {}

    # ------------------------------------------------------------- queries --
    @staticmethod
    def active_for(db: Session, service: str) -> Optional[Deployment]:
        return (
            db.execute(
                select(Deployment).where(
                    Deployment.service_name == service,
                    Deployment.status.in_([s.value for s in ACTIVE_STATES]),
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    def latest_for(db: Session, service: str) -> Optional[Deployment]:
        return (
            db.execute(
                select(Deployment)
                .where(Deployment.service_name == service)
                .order_by(Deployment.deployed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    # ---------------------------------------------------------- API calls --
    def create(
        self,
        db: Session,
        *,
        service_name: str,
        version: str,
        now: datetime,
        strategy: str = Strategy.CANARY.value,
        config_diff: Optional[dict] = None,
        image: Optional[str] = None,
        auto_promote: bool = True,
        simulate_failure: bool = False,
        previous_version_id: Optional[str] = None,
    ) -> Deployment:
        spec = catalog.get(service_name)
        if spec is None:
            raise UnknownService(service_name)
        with self.lock:
            active = self.active_for(db, service_name)
            if active is not None:
                raise DeploymentConflict(f"deployment {active.id} for {service_name} is still {active.status}")
            live = self.cluster.live_color(service_name) or "blue"
            target = m.other_color(live)
            diff = dict(config_diff or {})
            if simulate_failure:
                diff.setdefault("FAULT_ERROR_RATE", SIMULATED_FAILURE_ERROR_RATE)
            if previous_version_id is None:
                prev = (
                    db.execute(
                        select(Deployment)
                        .where(Deployment.service_name == service_name, Deployment.status == DeployState.LIVE.value)
                        .order_by(Deployment.deployed_at.desc())
                    )
                    .scalars()
                    .first()
                )
                previous_version_id = prev.id if prev else None
            dep = Deployment(
                service_name=service_name,
                version=version,
                status=DeployState.IDLE.value,
                config_diff=diff or None,
                previous_version_id=previous_version_id,
                deployed_at=now,
                k8s_namespace=self.cluster.namespace,
                k8s_deployment_name=m.deployment_name(service_name, target),
                image=image,
                strategy=Strategy(strategy).value,
                target_color=target,
                stable_color=live,
                auto_promote=auto_promote,
                stage_started_at=now,
                status_reason="queued",
                stage_history=[{"stage": DeployState.IDLE.value, "at": now.isoformat(), "reason": "created"}],
            )
            db.add(dep)
            db.commit()
            db.refresh(dep)
            return dep

    def promote(self, db: Session, dep: Deployment, now: datetime) -> StageDecision:
        with self.lock:
            state = DeployState(dep.status)
            if state not in CANARY_STATES:
                raise DeploymentConflict(f"deployment is {dep.status}, not in a canary stage")
            decision = self.canary.manual_promote(db, dep, now)
            self.last_verdict[dep.id] = decision.verdict.to_dict()
            if decision.action == "rollback":
                self._rollback(
                    db,
                    dep,
                    now,
                    self.rollback_engine.evaluate(dep.service_name, canary_gate_failure=decision.verdict.reason),
                    gate=decision.verdict.to_dict(),
                )
            elif decision.action == "promote":
                self._advance_canary(db, dep, now, decision, manual=True)
            return decision

    def manual_rollback(self, db: Session, dep: Deployment, now: datetime) -> Deployment:
        with self.lock:
            if dep.status == DeployState.ROLLED_BACK.value or dep.status == DeployState.ROLLING_BACK.value:
                raise DeploymentConflict("deployment already rolling back / rolled back")
            if dep.status == DeployState.IDLE.value:
                raise DeploymentConflict("deployment has not started yet; nothing to roll back")
            latest = self.latest_for(db, dep.service_name)
            if latest is not None and latest.id != dep.id:
                raise DeploymentConflict(
                    "only the latest deployment of a service can be rolled back; deploy the old version instead"
                )
            self._rollback(db, dep, now, self.rollback_engine.evaluate(dep.service_name, manual=True))
            return dep

    def rollback_live(self, db: Session, dep: Deployment, now: datetime, decision: RollbackDecision) -> None:
        """Post-deploy rollback of a LIVE release (control loop, FAST_BURN)."""
        with self.lock:
            self._rollback(db, dep, now, decision)

    # ------------------------------------------------------- reconciliation --
    def restore_routing(self, db: Session) -> None:
        """Rebuild the in-memory routing table from the cluster and the
        deployments table (after a restart)."""
        for spec in catalog.CATALOG:
            try:
                live = self.cluster.live_color(spec.name) or "blue"
            except Exception:  # noqa: BLE001 — cluster may be unreachable at boot
                live = "blue"
            self.routing.set_live(spec.name, live)
        for dep in db.execute(
            select(Deployment).where(Deployment.status.in_([s.value for s in CANARY_STATES]))
        ).scalars():
            self.routing.set_canary(
                dep.service_name, dep.stable_color, dep.target_color, CANARY_WEIGHT[DeployState(dep.status)]
            )

    def reconcile(self, db: Session, now: datetime) -> None:
        active = list(
            db.execute(
                select(Deployment)
                .where(Deployment.status.in_([s.value for s in ACTIVE_STATES]))
                .order_by(Deployment.deployed_at)
            ).scalars()
        )
        for dep in active:
            with self.lock:
                db.refresh(dep)
                try:
                    self._step(db, dep, now)
                except Exception as exc:  # noqa: BLE001 — one bad rollout must not stall the others
                    logger.exception("reconcile %s failed", dep.id)
                    db.rollback()
                    dep.status_reason = f"controller error: {exc}"
                    db.commit()

    def _step(self, db: Session, dep: Deployment, now: datetime) -> None:
        state = DeployState(dep.status)
        if state in (DeployState.IDLE, DeployState.PREPARING_GREEN):
            self._prepare(db, dep, now)
        elif state == DeployState.DEPLOYING_GREEN:
            self._await_rollout(db, dep, now)
        elif state == DeployState.HEALTH_CHECKING:
            self._health_gate(db, dep, now)
        elif state in CANARY_STATES:
            self._canary_stage(db, dep, now)
        elif state == DeployState.TRAFFIC_SWITCHING:
            self._switch(db, dep, now)
        elif state == DeployState.ROLLING_BACK:
            self._complete_rollback(db, dep, now)

    # -------------------------------------------------------------- states --
    def _transition(
        self,
        db: Session,
        dep: Deployment,
        to: DeployState,
        now: datetime,
        reason: str = "",
        gate: Optional[dict] = None,
    ) -> None:
        DeploymentStateMachine(DeployState(dep.status)).transition(to)  # raises on illegal jumps
        dep.status = to.value
        dep.status_reason = reason
        entry = {"stage": to.value, "at": now.isoformat(), "reason": reason}
        if gate is not None:
            entry["gate"] = gate
        dep.stage_history = [*(dep.stage_history or []), entry]
        db.commit()
        if to in (DeployState.LIVE, DeployState.ROLLED_BACK):
            observability.ROLLOUTS_FINISHED.labels(strategy=dep.strategy, status=to.value).inc()

    def _prepare(self, db: Session, dep: Deployment, now: datetime) -> None:
        if dep.status == DeployState.IDLE.value:
            self._transition(
                db, dep, DeployState.PREPARING_GREEN, now, f"applying {dep.version} to {dep.k8s_deployment_name}"
            )
        spec = catalog.get(dep.service_name)
        try:
            self.cluster.apply_version(
                spec, dep.target_color, dep.version, config_diff=dep.config_diff, image=dep.image
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as the rollback reason
            self._rollback(
                db, dep, now, self.rollback_engine.evaluate(dep.service_name, rollout_failure=f"apply failed: {exc}")
            )
            return
        dep.stage_started_at = now
        self._transition(db, dep, DeployState.DEPLOYING_GREEN, now, "waiting for the new ReplicaSet to become ready")

    def _await_rollout(self, db: Session, dep: Deployment, now: datetime) -> None:
        st = self.cluster.rollout_status(dep.service_name, dep.target_color)
        if st.complete:
            self._fatal_since.pop(dep.id, None)
            dep.stage_started_at = now
            self._health.pop(dep.id, None)
            self._transition(db, dep, DeployState.HEALTH_CHECKING, now, f"rollout complete: {st.message}")
            return
        failure = None
        if st.failed:
            failure = st.message
        elif st.fatal_reason:
            first = self._fatal_since.setdefault(dep.id, now)
            held = (now - first).total_seconds()
            if held >= settings.rollout_fast_fail_s:
                failure = f"{st.fatal_reason} for {held:.0f}s ({st.message})"
        else:
            self._fatal_since.pop(dep.id, None)
        started = dep.stage_started_at or dep.deployed_at
        if failure is None and (now - started).total_seconds() > settings.rollout_timeout_s + 30:
            failure = f"rollout not complete after {(now - started).total_seconds():.0f}s ({st.message})"
        if failure:
            self._rollback(db, dep, now, self.rollback_engine.evaluate(dep.service_name, rollout_failure=failure))
            return
        reason = st.message + (f" — pod state {st.fatal_reason}" if st.fatal_reason else "")
        if dep.status_reason != reason:
            dep.status_reason = reason
            db.commit()

    def _health_gate(self, db: Session, dep: Deployment, now: datetime) -> None:
        canary = dep.strategy == Strategy.CANARY.value
        checker = HealthChecker.canary_smoke() if canary else HealthChecker()
        probes = settings.canary_smoke_requests if canary else settings.health_probe_requests
        gate = self._health.setdefault(dep.id, HealthGate(checker))
        while True:
            outcomes = self.gateway.probe(dep.service_name, dep.target_color, probes)
            result = gate.observe(sample_from_outcomes(outcomes))
            if result is not None or not settings.health_check_fast:
                break
        if result is None:
            dep.status_reason = f"health poll {gate.polls}/{gate.checker.polls_needed} against {dep.target_color}"
            db.commit()
            return
        self._health.pop(dep.id, None)
        if not result.passed:
            decision = self.rollback_engine.evaluate(
                dep.service_name, consecutive_health_failures=result.consecutive_failures
            )
            self._rollback(db, dep, now, decision)
            return
        if dep.strategy == Strategy.BLUE_GREEN.value:
            self._transition(
                db, dep, DeployState.TRAFFIC_SWITCHING, now, f"health gate passed after {result.polls} polls"
            )
            self._switch(db, dep, now)
            return
        dep.stage_started_at = now
        self.routing.set_canary(
            dep.service_name, dep.stable_color, dep.target_color, CANARY_WEIGHT[DeployState.CANARY_5]
        )
        self._transition(
            db, dep, DeployState.CANARY_5, now, f"health gate passed; 5% of edge traffic to {dep.target_color}"
        )

    def _canary_stage(self, db: Session, dep: Deployment, now: datetime) -> None:
        decision = self.canary.evaluate(db, dep, now)
        self.last_verdict[dep.id] = decision.verdict.to_dict()
        if decision.action == "rollback":
            self._rollback(
                db,
                dep,
                now,
                self.rollback_engine.evaluate(dep.service_name, canary_gate_failure=decision.verdict.reason),
                gate=decision.verdict.to_dict(),
            )
        elif decision.action == "promote":
            self._advance_canary(db, dep, now, decision)
        else:
            reason = decision.verdict.reason
            if decision.action == "hold":
                reason = f"awaiting manual promotion — {reason}"
            if dep.status_reason != reason:
                dep.status_reason = reason
                db.commit()

    def _advance_canary(
        self, db: Session, dep: Deployment, now: datetime, decision: StageDecision, manual: bool = False
    ) -> None:
        nxt = decision.next_state
        how = "manually promoted" if manual else "gate passed"
        gate = decision.verdict.to_dict()
        if nxt == DeployState.TRAFFIC_SWITCHING:
            self._transition(db, dep, nxt, now, f"{how} at 100%: {decision.verdict.reason}", gate=gate)
            self._switch(db, dep, now)
            return
        weight = CANARY_WEIGHT[nxt]
        dep.stage_started_at = now
        self.routing.set_canary(dep.service_name, dep.stable_color, dep.target_color, weight)
        self._transition(
            db, dep, nxt, now, f"{how}: {decision.verdict.reason}; {weight}% to {dep.target_color}", gate=gate
        )

    def _switch(self, db: Session, dep: Deployment, now: datetime) -> None:
        self.cluster.switch_live(dep.service_name, dep.target_color)
        self.routing.set_live(dep.service_name, dep.target_color)
        dep.switched_at = now
        dep.finished_at = now
        self._transition(
            db,
            dep,
            DeployState.LIVE,
            now,
            f"live Service {dep.service_name} selector -> {dep.target_color}; "
            f"{dep.stable_color} kept warm for rollback",
        )
        logger.info("deployment %s LIVE (%s %s on %s)", dep.id, dep.service_name, dep.version, dep.target_color)

    # ------------------------------------------------------------ rollback --
    def _rollback(
        self, db: Session, dep: Deployment, now: datetime, decision: RollbackDecision, gate: Optional[dict] = None
    ) -> None:
        # Traffic first: every edge request goes back to the stable colour now.
        self.routing.set_live(dep.service_name, dep.stable_color)
        self._transition(db, dep, DeployState.ROLLING_BACK, now, decision.reason, gate=gate)
        self._health.pop(dep.id, None)
        self._fatal_since.pop(dep.id, None)

        severity = "CRITICAL" if decision.trigger == RollbackTrigger.SLO_FAST_BURN else "HIGH"
        context = {"trigger": decision.trigger.value if decision.trigger else None, "deployment_id": dep.id}
        if gate is not None:
            context["canary_gate"] = gate
        self.incidents.open(
            db,
            dep.service_name,
            f"Rollback of {dep.version}: {decision.reason}",
            severity=severity,
            now=now,
            graph=self.graph(db),
            deployment_id=dep.id,
            context=context,
            release_specific=self._release_specific(db, dep, now, decision, gate),
        )
        observability.ROLLBACKS.labels(trigger=decision.trigger.value if decision.trigger else "unknown").inc()
        if decision.trigger != RollbackTrigger.MANUAL:
            self.rollback_engine.start_cooldown(
                db, dep.service_name, now, trigger=decision.trigger, deployment_id=dep.id
            )
        try:
            self._complete_rollback(db, dep, now)
        except Exception as exc:  # noqa: BLE001 — ROLLING_BACK is retried next tick
            logger.exception("rollback of %s incomplete; will retry", dep.id)
            db.rollback()
            dep.status_reason = f"rollback in progress, retrying: {exc}"
            db.commit()

    def _release_specific(
        self, db: Session, dep: Deployment, now: datetime, decision: RollbackDecision, gate: Optional[dict]
    ) -> bool:
        """Does the evidence pin the failure on this release rather than on
        something it depends on? Yes when the candidate never served, or when
        it failed its gate while the stable colour, which calls the same
        dependencies, stayed under the page burn rate. Such an incident is
        never filed as a cascade of an upstream incident."""
        if decision.trigger == RollbackTrigger.ROLLOUT_FAILED:
            return True
        if decision.trigger not in (RollbackTrigger.CANARY_GATE_FAILED, RollbackTrigger.CONSECUTIVE_HEALTH_FAILURES):
            return False
        if gate is not None:
            stable = gate.get("stable_burn")
        else:
            burn = self.canary.burn
            counts = burn.window_counts(
                db, dep.service_name, dep.stage_started_at or dep.deployed_at, now, color=dep.stable_color
            )
            stable = burn_rate(counts.bad, counts.total, burn.slo(db, dep.service_name).slo_target)
        return stable is None or stable <= settings.canary_max_burn_rate

    def _complete_rollback(self, db: Session, dep: Deployment, now: datetime) -> None:
        """Idempotent: safe to retry on the next tick if a cluster call fails."""
        self.routing.set_live(dep.service_name, dep.stable_color)
        if self.cluster.live_color(dep.service_name) != dep.stable_color:
            self.cluster.switch_live(dep.service_name, dep.stable_color)
        self.cluster.scale(dep.service_name, dep.target_color, 0)
        dep.finished_at = now
        self._transition(
            db,
            dep,
            DeployState.ROLLED_BACK,
            now,
            f"traffic on {dep.stable_color}; {dep.target_color} scaled to 0",
        )

    def watch_window_open(self, dep: Deployment, now: datetime) -> bool:
        return (
            dep.status == DeployState.LIVE.value
            and dep.switched_at is not None
            and now - dep.switched_at <= timedelta(seconds=settings.post_deploy_watch_s)
        )
