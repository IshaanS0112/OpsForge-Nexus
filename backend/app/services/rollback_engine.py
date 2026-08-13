"""Rollback engine: concrete trigger conditions + anti-flapping cooldown.

Every trigger records the *exact* condition and value that fired, so an
incident's ``trigger_reason`` is auditable rather than "something looked bad".

Anti-flapping: after a rollback we suppress further
auto-rollback triggers for a cooldown window, otherwise a rollback can induce a
transient anomaly that triggers another rollback — a loop. The cooldown is kept
here in-memory for the MVP; a note in the docs explains prod would persist it
(e.g. Redis) so it survives restarts and is shared across workers.
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
    ERROR_RATE = "error_rate"
    LATENCY_P95 = "latency_p95"
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

    def start_cooldown(self, service: str) -> None:
        self._cooldown_until[service] = self._clock() + settings.rollback_cooldown_s
        logger.info("Cooldown started for %s (%ss)", service, settings.rollback_cooldown_s)

    # ---- trigger evaluation ----
    def evaluate(
        self,
        service: str,
        *,
        consecutive_health_failures: int = 0,
        error_rate: Optional[float] = None,
        error_rate_sustained_s: float = 0.0,
        latency_p95: Optional[float] = None,
        latency_baseline: Optional[float] = None,
        latency_sustained_s: float = 0.0,
        manual: bool = False,
    ) -> RollbackDecision:
        """Return a decision. Manual triggers bypass cooldown; automatic
        triggers are suppressed while the service is cooling down."""
        if manual:
            return RollbackDecision(True, RollbackTrigger.MANUAL, "Manual rollback initiated by user")

        if self.in_cooldown(service):
            logger.info("Auto-rollback suppressed for %s: in cooldown", service)
            return RollbackDecision(False, None, "suppressed: cooldown active")

        if consecutive_health_failures >= settings.health_consecutive_failures_to_rollback:
            return RollbackDecision(
                True,
                RollbackTrigger.CONSECUTIVE_HEALTH_FAILURES,
                f"{consecutive_health_failures} consecutive failed health checks "
                f"(>= {settings.health_consecutive_failures_to_rollback})",
            )

        if (
            error_rate is not None
            and error_rate > settings.rollback_error_rate_threshold
            and error_rate_sustained_s >= settings.rollback_error_rate_sustain_s
        ):
            return RollbackDecision(
                True,
                RollbackTrigger.ERROR_RATE,
                f"error_rate {error_rate:.1%} > {settings.rollback_error_rate_threshold:.0%} "
                f"sustained {error_rate_sustained_s:.0f}s",
            )

        if (
            latency_p95 is not None
            and latency_baseline
            and latency_p95 > settings.rollback_latency_multiplier * latency_baseline
            and latency_sustained_s >= settings.rollback_latency_sustain_s
        ):
            return RollbackDecision(
                True,
                RollbackTrigger.LATENCY_P95,
                f"p95 latency {latency_p95:.0f}ms > "
                f"{settings.rollback_latency_multiplier}x baseline "
                f"({latency_baseline:.0f}ms) sustained {latency_sustained_s:.0f}s",
            )

        return RollbackDecision(False, None, "all conditions within thresholds")
