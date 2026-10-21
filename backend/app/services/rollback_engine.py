"""Rollback engine: concrete trigger conditions + anti-flapping cooldown.

Every trigger records the *exact* condition and value that fired, so an
incident's ``trigger_reason`` is auditable rather than "something looked bad".

Triggers:

* Rollout-safety gates — the candidate never became Ready, failed its health
  gate, or failed a canary stage's burn-rate gate. These abort a rollout that
  is still in flight.
* ``SLO_FAST_BURN`` — the primary trigger for a release that is already LIVE
  (within the post-deploy watch window). It replaces V1's flat "error rate
  > 5% sustained 30s" threshold and fires only when all of these hold:
    - the FAST_BURN multiwindow alert is firing (1h and 5m both > 14.4x),
    - at least two anomaly-detector layers agree (consensus),
    - the service is not a cascade victim: rolling back a healthy release
      cannot fix an upstream failure.
* Manual — operator override.

Anti-flapping: after a rollback, metric-triggered rollbacks for that service
are suppressed for a cooldown window — a rollback can itself cause a
transient anomaly, and acting on it would loop. Rollout-safety gates are not
suppressed: a candidate that fails its own gate must be aborted, not left
half-rolled-out. (V1 applied the cooldown to its health gate too, which left
a failing deployment stuck in HEALTH_CHECKING.) The cooldown is in-memory;
production would keep it in shared storage so it survives restarts and is
shared across workers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models import RollbackCooldown

logger = logging.getLogger(__name__)


class RollbackTrigger(str, Enum):
    CONSECUTIVE_HEALTH_FAILURES = "consecutive_health_failures"
    SMOKE_CHECK_FAILED = "smoke_check_failed"
    ROLLOUT_FAILED = "rollout_failed"
    CANARY_GATE_FAILED = "canary_gate_failed"
    SLO_FAST_BURN = "slo_fast_burn"
    MANUAL = "manual"


@dataclass
class RollbackDecision:
    should_rollback: bool
    trigger: Optional[RollbackTrigger] = None
    reason: str = ""

    @property
    def as_reason(self) -> str:
        return self.reason


class RollbackEngine:
    """Decides rollbacks and keeps the anti-flapping cooldown.

    ``evaluate`` is a pure function of what the caller passes it, including
    whether the service is cooling down, so the decision is unit-testable
    without a database. The cooldown itself is a row in ``rollback_cooldowns``
    (V1 kept it in a process dictionary, which every restart forgot and no
    second worker could see)."""

    # ---- cooldown / anti-flapping ----
    def cooldown_remaining(self, db: Session, service: str, now: datetime) -> float:
        row = db.get(RollbackCooldown, service)
        return max(0.0, (row.until_at - now).total_seconds()) if row is not None else 0.0

    def in_cooldown(self, db: Session, service: str, now: datetime) -> bool:
        return self.cooldown_remaining(db, service, now) > 0.0

    def start_cooldown(
        self,
        db: Session,
        service: str,
        now: datetime,
        *,
        trigger: Optional[RollbackTrigger] = None,
        deployment_id: Optional[str] = None,
    ) -> None:
        row = db.get(RollbackCooldown, service)
        if row is None:
            row = RollbackCooldown(service_name=service)
            db.add(row)
        row.started_at = now
        row.until_at = now + timedelta(seconds=settings.rollback_cooldown_s)
        row.trigger = trigger.value if trigger else None
        row.deployment_id = deployment_id
        db.commit()
        logger.info("cooldown started for %s (%ss)", service, settings.rollback_cooldown_s)

    # ---- trigger evaluation ----
    def evaluate(
        self,
        service: str,
        *,
        health_gate_failure: Optional[str] = None,
        smoke_check_failure: Optional[str] = None,
        rollout_failure: Optional[str] = None,
        canary_gate_failure: Optional[str] = None,
        burn_severity: Optional[str] = None,
        burn_rate: Optional[float] = None,
        consensus_layers: int = 0,
        cascade_root: Optional[str] = None,
        in_cooldown: bool = False,
        manual: bool = False,
    ) -> RollbackDecision:
        """Return a decision. Manual and rollout-safety triggers always act;
        the metric trigger is suppressed while the service cools down
        (``in_cooldown``, which the caller reads from the database)."""
        if manual:
            return RollbackDecision(True, RollbackTrigger.MANUAL, "Manual rollback initiated by user")

        if rollout_failure:
            return RollbackDecision(True, RollbackTrigger.ROLLOUT_FAILED, f"rollout failed: {rollout_failure}")

        # The gate applies its own fail limit and hands over the verdict. The
        # engine does not re-derive it: duplicating the threshold here is how a
        # canary's one-poll smoke check once failed with "no condition met".
        if smoke_check_failure:
            return RollbackDecision(True, RollbackTrigger.SMOKE_CHECK_FAILED, smoke_check_failure)

        if health_gate_failure:
            return RollbackDecision(True, RollbackTrigger.CONSECUTIVE_HEALTH_FAILURES, health_gate_failure)

        if canary_gate_failure:
            return RollbackDecision(
                True, RollbackTrigger.CANARY_GATE_FAILED, f"canary gate failed: {canary_gate_failure}"
            )

        if burn_severity != "FAST_BURN":
            return RollbackDecision(False, None, "no rollback condition met")
        if in_cooldown:
            logger.info("auto-rollback suppressed for %s: in cooldown", service)
            return RollbackDecision(False, None, "suppressed: cooldown active")
        if cascade_root and cascade_root != service:
            return RollbackDecision(
                False, None, f"FAST_BURN is a cascade from {cascade_root}; rolling back {service} would not fix it"
            )
        if consensus_layers < settings.consensus_min_layers:
            return RollbackDecision(
                False,
                None,
                f"FAST_BURN without detector consensus "
                f"({consensus_layers}/{settings.consensus_min_layers} layers): page only",
            )
        rate = f" at {burn_rate:.1f}x" if burn_rate is not None else ""
        return RollbackDecision(
            True,
            RollbackTrigger.SLO_FAST_BURN,
            f"FAST_BURN{rate} (1h and 5m windows > 14.4x) with {consensus_layers}/3 detector layers agreeing",
        )
