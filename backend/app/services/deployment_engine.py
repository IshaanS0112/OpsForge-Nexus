"""Blue-green deployment engine.

State machine (interview question #1):

    IDLE -> PREPARING_GREEN -> DEPLOYING_GREEN -> HEALTH_CHECKING
         -> TRAFFIC_SWITCHING -> LIVE
                    |
                    v  (health gate fails)
              ROLLING_BACK -> ROLLED_BACK

The state machine and the health gate are *real* logic. What is simulated is
the infrastructure: there is no K8s cluster; traffic switching is a flag flip
and health samples come from a pluggable sampler. That boundary is deliberate
and documented (see "What NOT to Build").
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)


class DeployState(str, Enum):
    IDLE = "IDLE"
    PREPARING_GREEN = "PREPARING_GREEN"
    DEPLOYING_GREEN = "DEPLOYING_GREEN"
    HEALTH_CHECKING = "HEALTH_CHECKING"
    TRAFFIC_SWITCHING = "TRAFFIC_SWITCHING"
    LIVE = "LIVE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


# Allowed transitions — anything not listed is an illegal jump and raises.
_TRANSITIONS: dict[DeployState, set[DeployState]] = {
    DeployState.IDLE: {DeployState.PREPARING_GREEN},
    DeployState.PREPARING_GREEN: {DeployState.DEPLOYING_GREEN, DeployState.ROLLING_BACK},
    DeployState.DEPLOYING_GREEN: {DeployState.HEALTH_CHECKING, DeployState.ROLLING_BACK},
    DeployState.HEALTH_CHECKING: {DeployState.TRAFFIC_SWITCHING, DeployState.ROLLING_BACK},
    DeployState.TRAFFIC_SWITCHING: {DeployState.LIVE, DeployState.ROLLING_BACK},
    DeployState.LIVE: {DeployState.ROLLING_BACK},
    DeployState.ROLLING_BACK: {DeployState.ROLLED_BACK},
    DeployState.ROLLED_BACK: set(),
}


class IllegalTransition(Exception):
    pass


class DeploymentStateMachine:
    """Pure, DB-free state machine. Unit tested in isolation."""

    def __init__(self, state: DeployState = DeployState.IDLE) -> None:
        self.state = state
        self.history: list[DeployState] = [state]

    def can_transition(self, to: DeployState) -> bool:
        return to in _TRANSITIONS[self.state]

    def transition(self, to: DeployState) -> DeployState:
        if not self.can_transition(to):
            raise IllegalTransition(f"{self.state.value} -> {to.value} is not allowed")
        self.state = to
        self.history.append(to)
        return self.state


@dataclass
class HealthSample:
    success_rate: float          # fraction of 200s, 0..1
    p95_latency_ms: float
    has_5xx_spike: bool = False


@dataclass
class HealthCheckResult:
    passed: bool
    consecutive_failures: int
    polls: int
    reason: str


# A sampler returns a HealthSample for a given poll index. In prod this would
# hit the green environment's health endpoint; in the MVP it's simulated.
Sampler = Callable[[int], HealthSample]


class HealthChecker:
    """Polls a sampler over the configured window; a poll passes only if
    success_rate >= threshold AND p95 < max AND no 5xx spike. Three consecutive
    failed polls fail the gate (-> rollback)."""

    def __init__(
        self,
        interval_s: int = settings.health_check_interval_s,
        window_s: int = settings.health_check_window_s,
        min_success_rate: float = settings.health_min_success_rate,
        max_p95_ms: float = settings.health_max_p95_latency_ms,
        fail_limit: int = settings.health_consecutive_failures_to_rollback,
    ) -> None:
        self.interval_s = interval_s
        self.window_s = window_s
        self.min_success_rate = min_success_rate
        self.max_p95_ms = max_p95_ms
        self.fail_limit = fail_limit

    def poll_passes(self, s: HealthSample) -> bool:
        return (
            s.success_rate >= self.min_success_rate
            and s.p95_latency_ms < self.max_p95_ms
            and not s.has_5xx_spike
        )

    def run(self, sampler: Sampler, sleep: Callable[[float], None] = time.sleep) -> HealthCheckResult:
        """Poll the sampler over the window, sleeping ``interval_s`` between
        polls so the gate is genuinely time-based. Set ``settings.health_check_fast``
        to skip the sleeps (tests/demo). ``sleep`` is injectable for tests."""
        polls = max(1, self.window_s // self.interval_s)
        do_sleep = not settings.health_check_fast
        consecutive_failures = 0
        for i in range(polls):
            sample = sampler(i)
            if self.poll_passes(sample):
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= self.fail_limit:
                    return HealthCheckResult(
                        passed=False,
                        consecutive_failures=consecutive_failures,
                        polls=i + 1,
                        reason=f"{consecutive_failures} consecutive failed health checks",
                    )
            if do_sleep and i < polls - 1:
                sleep(self.interval_s)
        return HealthCheckResult(
            passed=True,
            consecutive_failures=0,
            polls=polls,
            reason="health gate passed",
        )


# ---- simulated samplers used by the API demo path ----
def healthy_sampler(_: int) -> HealthSample:
    return HealthSample(success_rate=0.999, p95_latency_ms=180.0, has_5xx_spike=False)


def failing_sampler(i: int) -> HealthSample:
    # first poll looks fine, then it degrades — realistic bad-deploy shape
    if i == 0:
        return HealthSample(success_rate=0.995, p95_latency_ms=220.0)
    return HealthSample(success_rate=0.80, p95_latency_ms=1200.0, has_5xx_spike=True)


def make_sampler(simulate_failure: bool) -> Sampler:
    return failing_sampler if simulate_failure else healthy_sampler
