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
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class RollbackTrigger(str, Enum):
    CONSECUTIVE_HEALTH_FAILURES = "consecutive_health_failures"
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
    """Evaluates rollback conditions and enforces a per-service cooldown."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}

    # ---- cooldown / anti-flapping ----
    def in_cooldown(self, service: str) -> bool:
        until = self._cooldown_until.get(service, 0.0)
        return self._clock() < until

    def cooldown_remaining(self, service: str) -> float:
        return max(0.0, self._cooldown_until.get(service, 0.0) - self._clock())

    def start_cooldown(self, service: str) -> None:
        self._cooldown_until[service] = self._clock() + settings.rollback_cooldown_s
        logger.info("Cooldown started for %s (%ss)", service, settings.rollback_cooldown_s)

    # ---- trigger evaluation ----
    def evaluate(
        self,
        service: str,
        *,
        consecutive_health_failures: int = 0,
        rollout_failure: Optional[str] = None,
        canary_gate_failure: Optional[str] = None,
        burn_severity: Optional[str] = None,
        burn_rate: Optional[float] = None,
        consensus_layers: int = 0,
        cascade_root: Optional[str] = None,
        manual: bool = False,
    ) -> RollbackDecision:
        """Return a decision. Manual and rollout-safety triggers always act;
        the metric trigger is suppressed while the service cools down."""
        if manual:
            return RollbackDecision(True, RollbackTrigger.MANUAL, "Manual rollback initiated by user")

        if rollout_failure:
            return RollbackDecision(True, RollbackTrigger.ROLLOUT_FAILED, f"rollout failed: {rollout_failure}")

        if consecutive_health_failures >= settings.health_consecutive_failures_to_rollback:
            return RollbackDecision(
                True,
                RollbackTrigger.CONSECUTIVE_HEALTH_FAILURES,
                f"{consecutive_health_failures} consecutive failed health checks "
                f"(>= {settings.health_consecutive_failures_to_rollback})",
            )

        if canary_gate_failure:
            return RollbackDecision(
                True, RollbackTrigger.CANARY_GATE_FAILED, f"canary gate failed: {canary_gate_failure}"
            )

        if burn_severity != "FAST_BURN":
            return RollbackDecision(False, None, "no rollback condition met")
        if self.in_cooldown(service):
            logger.info("Auto-rollback suppressed for %s: in cooldown", service)
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
