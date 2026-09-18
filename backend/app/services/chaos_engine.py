"""Chaos drills: inject a real fault, measure whether the system catches it.

Chaos Engineering in the Chaos Monkey / *Principles of Chaos Engineering*
sense: break something on purpose inside the kind cluster and measure the
reliability machinery's response instead of trusting it.

    POST /chaos/inject {service_name, fault_type, magnitude, target}

    LATENCY_INJECTION  every targeted pod adds ``magnitude`` ms to /work
    ERROR_BURST        every targeted pod fails ``magnitude`` (0..1) of /work with a 500
    POD_KILL           delete ``magnitude`` (0..1, share) of the targeted pods;
                       their ReplicaSet replaces them, and each replacement
                       warms up before it is Ready again

    target: "all" pods of the service, the "stable" colour, or the "canary"
    colour of an in-flight canary (a deployment-linked drill: the fault stands
    in for a bad release, so a rollback is expected).

Faults are applied through the API server's pod proxy (``/admin/fault``) and
expire on their own inside the pod after ``duration_s``, so a drill cannot
leave a fault behind even if the backend dies.

Measured on the control loop's own signals — the drill never tells the
detectors anything:

    detection_latency_seconds  injection -> the first of the system's own
                               detection mechanisms firing for the service:
                               anomaly consensus (>= 2 layers agree), a
                               FAST_BURN alert, or — for a canary-targeted
                               drill — the canary burn-rate gate failing
    rollback_latency_seconds   injection -> linked deployment ROLLED_BACK
    recovery_latency_seconds   injection -> service quiet again after the fault ends

    passed = detection within detection_bound_s
             AND (rollback within rollback_bound_s, if deployment-linked)

The fault runs for its full ``duration_s`` so every downstream mechanism
(incidents, cascade classification, rollbacks) gets exercised, then the drill
waits for the service to recover. Each detection mechanism's first firing is
recorded on the timeline, not just the earliest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog
from app.config import settings
from app.models import ChaosExperiment, Deployment
from app.services.deployment_engine import CANARY_STATES, DeployState

logger = logging.getLogger(__name__)

FAULT_TYPES = {
    "LATENCY_INJECTION": {"kind": "latency", "default": 800.0, "min": 50.0, "max": 5000.0, "unit": "ms"},
    "ERROR_BURST": {"kind": "error", "default": 0.5, "min": 0.05, "max": 1.0, "unit": "fraction of requests"},
    "POD_KILL": {"kind": None, "default": 1.0, "min": 0.01, "max": 1.0, "unit": "fraction of pods"},
}
TARGETS = ("all", "stable", "canary")


class ChaosError(ValueError):
    pass


def evaluate_drill(
    detection_s: Optional[float],
    rollback_s: Optional[float],
    deployment_linked: bool,
    detection_bound_s: float,
    rollback_bound_s: float,
) -> tuple[bool, str]:
    """Pure pass/fail with a human-readable verdict."""
    problems = []
    if detection_s is None:
        problems.append(f"not detected within {detection_bound_s:.0f}s")
    elif detection_s > detection_bound_s:
        problems.append(f"detected after {detection_s:.1f}s (bound {detection_bound_s:.0f}s)")
    if deployment_linked:
        if rollback_s is None:
            problems.append(f"no rollback within {rollback_bound_s:.0f}s")
        elif rollback_s > rollback_bound_s:
            problems.append(f"rolled back after {rollback_s:.1f}s (bound {rollback_bound_s:.0f}s)")
    if problems:
        return False, "FAIL: " + "; ".join(problems)
    parts = [f"detected in {detection_s:.1f}s"]
    if deployment_linked:
        parts.append(f"rolled back in {rollback_s:.1f}s")
    return True, "PASS: " + ", ".join(parts)


class ChaosEngine:
    def __init__(self, cluster, notifier=None) -> None:
        self.cluster = cluster
        self.notifier = notifier
        self._seen: dict[str, dict[str, float]] = {}  # drill -> mechanism -> first seen (s after injection)
        self._cleared: set[str] = set()

    # ------------------------------------------------------------- inject --
    def inject(
        self,
        db: Session,
        *,
        service_name: str,
        fault_type: str,
        now: datetime,
        magnitude: Optional[float] = None,
        target: str = "all",
        duration_s: Optional[int] = None,
        detection_bound_s: Optional[float] = None,
        rollback_bound_s: Optional[float] = None,
    ) -> ChaosExperiment:
        spec = catalog.get(service_name)
        if spec is None:
            raise ChaosError(f"unknown service {service_name!r}")
        fault = FAULT_TYPES.get(fault_type)
        if fault is None:
            raise ChaosError(f"fault_type must be one of {sorted(FAULT_TYPES)}")
        if target not in TARGETS:
            raise ChaosError(f"target must be one of {TARGETS}")
        magnitude = fault["default"] if magnitude is None else float(magnitude)
        if not fault["min"] <= magnitude <= fault["max"]:
            raise ChaosError(f"{fault_type} magnitude must be in [{fault['min']}, {fault['max']}] {fault['unit']}")
        running = (
            db.execute(
                select(ChaosExperiment).where(
                    ChaosExperiment.service_name == service_name, ChaosExperiment.status == "RUNNING"
                )
            )
            .scalars()
            .first()
        )
        if running is not None:
            raise ChaosError(f"drill {running.id} is already running on {service_name}")

        deployment: Optional[Deployment] = None
        live = self.cluster.live_color(service_name)
        if target == "canary":
            deployment = (
                db.execute(
                    select(Deployment).where(
                        Deployment.service_name == service_name,
                        Deployment.status.in_([s.value for s in CANARY_STATES]),
                    )
                )
                .scalars()
                .first()
            )
            if deployment is None:
                raise ChaosError(f"target 'canary' needs an in-flight canary deployment of {service_name}")
            colors = [deployment.target_color]
        elif target == "stable":
            colors = [live]
        else:
            colors = ["blue", "green"]

        pods = [p for p in self.cluster.list_pods(service_name) if p.color in colors and p.ready and not p.terminating]
        if not pods:
            raise ChaosError(f"no ready pods of {service_name} for target {target!r}")
        duration = int(duration_s or settings.chaos_default_duration_s)

        affected: list[dict] = []
        if fault["kind"] is not None:
            for p in pods:
                code, _ = self.cluster.pod_request(
                    p.name,
                    "POST",
                    "admin/fault",
                    {"type": fault["kind"], "magnitude": magnitude, "duration_s": duration},
                )
                affected.append({"pod": p.name, "color": p.color, "applied": code == 200})
        else:
            victims = pods[: max(1, round(magnitude * len(pods)))]
            for p in victims:
                self.cluster.delete_pod(p.name)
                affected.append({"pod": p.name, "color": p.color, "applied": True})
        if not any(a["applied"] for a in affected):
            raise ChaosError("fault could not be applied to any pod")

        exp = ChaosExperiment(
            service_name=service_name,
            fault_type=fault_type,
            injected_at=now,
            magnitude=magnitude,
            target=target,
            duration_s=duration,
            status="RUNNING",
            deployment_id=deployment.id if deployment else None,
            detection_bound_s=detection_bound_s or settings.chaos_detection_bound_s,
            rollback_bound_s=rollback_bound_s or settings.chaos_rollback_bound_s,
            affected_pods=affected,
            timeline=[
                {
                    "at": now.isoformat(),
                    "t": 0.0,
                    "event": (
                        f"injected {fault_type} ({magnitude:g} {fault['unit']}) "
                        f"into {len(affected)} pod(s), target={target}"
                    ),
                }
            ],
        )
        db.add(exp)
        db.commit()
        db.refresh(exp)
        logger.info("chaos drill %s: %s on %s (%s)", exp.id, fault_type, service_name, target)
        return exp

    # -------------------------------------------------------------- watch --
    def running(self, db: Session) -> list[ChaosExperiment]:
        return list(db.execute(select(ChaosExperiment).where(ChaosExperiment.status == "RUNNING")).scalars())

    def running_services(self, db: Session) -> dict[str, ChaosExperiment]:
        return {e.service_name: e for e in self.running(db)}

    @staticmethod
    def _event(exp: ChaosExperiment, now: datetime, event: str) -> None:
        t = (now - exp.injected_at).total_seconds()
        exp.timeline = [*(exp.timeline or []), {"at": now.isoformat(), "t": round(t, 1), "event": event}]

    def _clear_fault(self, exp: ChaosExperiment, now: datetime, why: str) -> None:
        if exp.id in self._cleared:
            return
        self._cleared.add(exp.id)
        if FAULT_TYPES[exp.fault_type]["kind"] is not None:
            for a in exp.affected_pods or []:
                try:
                    self.cluster.pod_request(a["pod"], "DELETE", "admin/fault")
                except Exception as exc:  # noqa: BLE001 — the pod-side TTL clears it anyway
                    logger.warning("clear fault on %s failed: %s", a["pod"], exc)
        self._event(exp, now, f"fault cleared ({why})")

    def _pods_ready(self, exp: ChaosExperiment) -> bool:
        colors = {a["color"] for a in exp.affected_pods or []}
        pods = [p for p in self.cluster.list_pods(exp.service_name) if p.color in colors]
        return bool(pods) and all(p.ready for p in pods if not p.terminating)

    def watch(
        self,
        db: Session,
        now: datetime,
        agreeing: dict[str, int],
        quiet: dict[str, bool],
        fast_burn: frozenset[str] = frozenset(),
    ) -> list[ChaosExperiment]:
        """Advance every running drill. ``agreeing`` is this tick's detector
        agreement per service, ``fast_burn`` the services with a firing
        FAST_BURN alert, ``quiet`` whether each service is healthy again.
        Returns the drills that completed this tick."""
        done = []
        for exp in self.running(db):
            elapsed = (now - exp.injected_at).total_seconds()
            seen = self._seen.setdefault(exp.id, {})
            layers = agreeing.get(exp.service_name, 0)
            dep = db.get(Deployment, exp.deployment_id) if exp.deployment_id else None

            if layers >= 1 and "signal" not in seen:
                seen["signal"] = elapsed
                self._event(exp, now, f"first signal: {layers}/3 anomaly layers")
            if layers >= settings.consensus_min_layers and "consensus" not in seen:
                seen["consensus"] = elapsed
                self._event(exp, now, f"detected by anomaly consensus ({layers}/3 layers agree)")
            if exp.service_name in fast_burn and "fast_burn" not in seen:
                seen["fast_burn"] = elapsed
                self._event(exp, now, "detected by FAST_BURN alert (1h and 5m windows > 14.4x)")
            if dep is not None and "gate" not in seen:
                gate_fail = next(
                    (
                        h
                        for h in dep.stage_history or []
                        if h.get("stage") == DeployState.ROLLING_BACK.value and "canary gate" in (h.get("reason") or "")
                    ),
                    None,
                )
                if gate_fail is not None:
                    seen["gate"] = (datetime.fromisoformat(gate_fail["at"]) - exp.injected_at).total_seconds()
                    self._event(exp, now, "detected by canary burn-rate gate")
            detections = [v for k, v in seen.items() if k in ("consensus", "fast_burn", "gate")]
            if detections and exp.detection_latency_seconds is None:
                exp.detection_latency_seconds = round(max(0.0, min(detections)), 2)
            if dep is not None and exp.rollback_latency_seconds is None and dep.status == DeployState.ROLLED_BACK.value:
                finished = dep.finished_at or now
                exp.rollback_latency_seconds = round((finished - exp.injected_at).total_seconds(), 2)
                self._event(exp, now, f"rollback complete: {dep.status_reason or ''}".strip())

            if elapsed >= (exp.duration_s or 0):
                self._clear_fault(exp, now, "duration elapsed")
            if exp.id not in self._cleared:
                continue
            recovered = quiet.get(exp.service_name, False)
            if exp.fault_type == "POD_KILL":
                recovered = recovered and self._pods_ready(exp)
            timed_out = elapsed > max(exp.detection_bound_s, exp.rollback_bound_s, exp.duration_s or 0) + 120
            if not (recovered or timed_out):
                continue
            if recovered:
                exp.recovery_latency_seconds = round(elapsed, 2)
                self._event(exp, now, "recovered: service healthy again")
            passed, verdict = evaluate_drill(
                exp.detection_latency_seconds,
                exp.rollback_latency_seconds,
                bool(exp.deployment_id),
                exp.detection_bound_s,
                exp.rollback_bound_s,
            )
            exp.passed = passed
            mechanisms = ", ".join(
                f"{k.replace('_', ' ')} at {v:.1f}s" for k, v in sorted(seen.items(), key=lambda kv: kv[1])
            )
            exp.notes = verdict + (f" ({mechanisms})" if mechanisms else "")
            exp.status = "COMPLETED"
            exp.completed_at = now
            self._cleared.discard(exp.id)
            self._seen.pop(exp.id, None)
            self._event(exp, now, verdict)
            done.append(exp)
            if self.notifier is not None:
                self.notifier.send(
                    {
                        "event": "chaos_drill_completed",
                        "experiment_id": exp.id,
                        "service_name": exp.service_name,
                        "fault_type": exp.fault_type,
                        "passed": passed,
                        "notes": exp.notes,
                    }
                )
        db.commit()
        return done

    def abort(self, db: Session, exp: ChaosExperiment, now: datetime) -> ChaosExperiment:
        if exp.status != "RUNNING":
            raise ChaosError("drill is not running")
        self._clear_fault(exp, now, "aborted by operator")
        exp.status = "COMPLETED"
        exp.completed_at = now
        exp.passed = None
        exp.notes = "aborted by operator"
        self._cleared.discard(exp.id)
        self._seen.pop(exp.id, None)
        db.commit()
        return exp

    @staticmethod
    def recent(db: Session, limit: int = 50) -> list[ChaosExperiment]:
        return list(
            db.execute(select(ChaosExperiment).order_by(ChaosExperiment.injected_at.desc()).limit(limit)).scalars()
        )

    @staticmethod
    def duration_left(exp: ChaosExperiment, now: datetime) -> float:
        return max(0.0, ((exp.injected_at + timedelta(seconds=exp.duration_s or 0)) - now).total_seconds())
