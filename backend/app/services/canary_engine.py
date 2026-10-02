"""Canary rollout engine: staged exposure 5% -> 25% -> 50% -> 100%.

At each stage the gateway sends that share of edge traffic to the candidate
colour. The stage is gated by the canary's own SLO burn rate over a
confirmation window (``burn_rate_engine.evaluate_canary_gate``):

* PASS  -> promote to the next stage (or, after 100%, switch the live
           Service selector so east-west traffic moves too);
* WAIT  -> keep collecting traffic;
* FAIL  -> roll back at once: 100% of traffic returns to the stable colour,
           not merely "stop promoting".

With ``auto_promote`` off, a passing stage holds until an operator calls
``POST /deployments/{id}/promote-canary``, which re-checks the gate first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app import observability
from app.config import settings
from app.models import Deployment
from app.services.burn_rate_engine import BurnRateService, GateVerdict
from app.services.deployment_engine import CANARY_WEIGHT, DeployState, next_canary_state


@dataclass
class StageDecision:
    action: str  # "promote" | "wait" | "hold" | "rollback"
    verdict: GateVerdict
    next_state: Optional[DeployState] = None


class CanaryEngine:
    def __init__(self, burn: BurnRateService, window_s: float = settings.canary_stage_window_s) -> None:
        self.burn = burn
        self.window_s = window_s

    @staticmethod
    def weight(state: DeployState) -> int:
        return CANARY_WEIGHT.get(state, 0)

    def verdict(self, db: Session, dep: Deployment, now: datetime, *, ignore_window: bool = False) -> GateVerdict:
        return self.burn.gate(
            db,
            dep.service_name,
            dep.target_color,
            dep.stable_color,
            dep.stage_started_at or now,
            now,
            window_s=0.0 if ignore_window else self.window_s,
        )

    def evaluate(self, db: Session, dep: Deployment, now: datetime) -> StageDecision:
        state = DeployState(dep.status)
        v = self.verdict(db, dep, now)
        observability.GATE_VERDICTS.labels(verdict=v.verdict).inc()
        if v.verdict == "FAIL":
            return StageDecision("rollback", v, DeployState.ROLLING_BACK)
        if v.verdict == "WAIT":
            return StageDecision("wait", v)
        if not dep.auto_promote:
            return StageDecision("hold", v)
        return StageDecision("promote", v, next_canary_state(state))

    def manual_promote(self, db: Session, dep: Deployment, now: datetime) -> StageDecision:
        """Operator promotion: skips the rest of the confirmation window but
        still requires enough canary traffic and no confident regression."""
        v = self.verdict(db, dep, now, ignore_window=True)
        if v.verdict == "FAIL":
            return StageDecision("rollback", v, DeployState.ROLLING_BACK)
        if v.verdict == "WAIT":
            return StageDecision("wait", v)
        return StageDecision("promote", v, next_canary_state(DeployState(dep.status)))
