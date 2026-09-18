"""Deployment state machine and health gate.

Blue-green and canary share one state machine:

    IDLE -> PREPARING_GREEN -> DEPLOYING_GREEN -> HEALTH_CHECKING
         -> (blue-green) TRAFFIC_SWITCHING -> LIVE
         -> (canary)     CANARY_5 -> CANARY_25 -> CANARY_50 -> CANARY_100
                         -> TRAFFIC_SWITCHING -> LIVE
    any non-terminal state, and LIVE -> ROLLING_BACK -> ROLLED_BACK

"GREEN" names the *candidate* environment: the colour the new version is
rolled out to. Physically it alternates between the ``-blue`` and ``-green``
Deployments; ``deployments.target_color`` records which one.

The machine is pure and DB-free. What drives it is ``deployment_controller``:
PREPARING applies the new version to the candidate Deployment, DEPLOYING
polls the real rollout status, HEALTH_CHECKING runs this module's health
gate against the candidate colour, canary stages are gated by
``canary_engine``, and TRAFFIC_SWITCHING patches the live Service selector.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.config import settings


class DeployState(str, Enum):
    IDLE = "IDLE"
    PREPARING_GREEN = "PREPARING_GREEN"
    DEPLOYING_GREEN = "DEPLOYING_GREEN"
    HEALTH_CHECKING = "HEALTH_CHECKING"
    CANARY_5 = "CANARY_5"
    CANARY_25 = "CANARY_25"
    CANARY_50 = "CANARY_50"
    CANARY_100 = "CANARY_100"
    TRAFFIC_SWITCHING = "TRAFFIC_SWITCHING"
    LIVE = "LIVE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class Strategy(str, Enum):
    CANARY = "CANARY"
    BLUE_GREEN = "BLUE_GREEN"


CANARY_STATES = (DeployState.CANARY_5, DeployState.CANARY_25, DeployState.CANARY_50, DeployState.CANARY_100)
CANARY_WEIGHT = {
    DeployState.CANARY_5: 5,
    DeployState.CANARY_25: 25,
    DeployState.CANARY_50: 50,
    DeployState.CANARY_100: 100,
}
TERMINAL_STATES = frozenset({DeployState.LIVE, DeployState.ROLLED_BACK})
# States in which a rollout is still in flight (one per service at a time).
ACTIVE_STATES = frozenset(set(DeployState) - TERMINAL_STATES)

_rb = DeployState.ROLLING_BACK
# Allowed transitions — anything not listed is an illegal jump and raises.
_TRANSITIONS: dict[DeployState, set[DeployState]] = {
    DeployState.IDLE: {DeployState.PREPARING_GREEN},
    DeployState.PREPARING_GREEN: {DeployState.DEPLOYING_GREEN, _rb},
    DeployState.DEPLOYING_GREEN: {DeployState.HEALTH_CHECKING, _rb},
    DeployState.HEALTH_CHECKING: {DeployState.TRAFFIC_SWITCHING, DeployState.CANARY_5, _rb},
    DeployState.CANARY_5: {DeployState.CANARY_25, _rb},
    DeployState.CANARY_25: {DeployState.CANARY_50, _rb},
    DeployState.CANARY_50: {DeployState.CANARY_100, _rb},
    DeployState.CANARY_100: {DeployState.TRAFFIC_SWITCHING, _rb},
    DeployState.TRAFFIC_SWITCHING: {DeployState.LIVE, _rb},
    DeployState.LIVE: {_rb},
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


def next_canary_state(state: DeployState) -> DeployState:
    """CANARY_5 -> CANARY_25 -> CANARY_50 -> CANARY_100 -> TRAFFIC_SWITCHING."""
    order = list(CANARY_STATES)
    i = order.index(state)
    return order[i + 1] if i + 1 < len(order) else DeployState.TRAFFIC_SWITCHING


@dataclass
class HealthSample:
    success_rate: float  # fraction of 200s, 0..1
    p95_latency_ms: float
    has_5xx_spike: bool = False


@dataclass
class HealthCheckResult:
    passed: bool
    consecutive_failures: int
    polls: int
    reason: str


# A sampler returns a HealthSample for a given poll index. The deployment
# controller's sampler sends smoke requests to the candidate colour's Service.
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
        spike_check: bool = True,
    ) -> None:
        self.interval_s = interval_s
        self.window_s = window_s
        self.min_success_rate = min_success_rate
        self.max_p95_ms = max_p95_ms
        self.fail_limit = fail_limit
        self.spike_check = spike_check

    @classmethod
    def canary_smoke(cls) -> HealthChecker:
        """One poll before any user traffic: is the candidate serving at all?
        Quality is then judged by the canary stages' burn-rate gates."""
        return cls(
            interval_s=1,
            window_s=1,
            min_success_rate=settings.canary_smoke_min_success_rate,
            max_p95_ms=float("inf"),
            fail_limit=1,
            spike_check=False,
        )

    @property
    def polls_needed(self) -> int:
        return max(1, self.window_s // self.interval_s)

    def poll_passes(self, s: HealthSample) -> bool:
        return (
            s.success_rate >= self.min_success_rate
            and s.p95_latency_ms < self.max_p95_ms
            and not (self.spike_check and s.has_5xx_spike)
        )

    def run(self, sampler: Sampler, sleep: Callable[[float], None] = time.sleep) -> HealthCheckResult:
        """Poll the sampler over the window, sleeping ``interval_s`` between
        polls so the gate is genuinely time-based. Set ``settings.health_check_fast``
        to skip the sleeps (tests/demo). ``sleep`` is injectable for tests."""
        gate = HealthGate(self)
        do_sleep = not settings.health_check_fast
        for i in range(self.polls_needed):
            result = gate.observe(sampler(i))
            if result is not None:
                return result
            if do_sleep and i < self.polls_needed - 1:
                sleep(self.interval_s)
        return gate.finish()


@dataclass
class HealthGate:
    """Incremental form of ``HealthChecker.run`` — one poll per call — so the
    control loop can spread the gate across ticks instead of blocking."""

    checker: HealthChecker = field(default_factory=HealthChecker)
    polls: int = 0
    consecutive_failures: int = 0

    def observe(self, sample: HealthSample) -> Optional[HealthCheckResult]:
        """Record one poll. Returns a failed result once the fail limit is hit,
        a passed result after the last poll of the window, else None."""
        self.polls += 1
        if self.checker.poll_passes(sample):
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.checker.fail_limit:
                return HealthCheckResult(
                    passed=False,
                    consecutive_failures=self.consecutive_failures,
                    polls=self.polls,
                    reason=f"{self.consecutive_failures} consecutive failed health checks",
                )
        if self.polls >= self.checker.polls_needed:
            return self.finish()
        return None

    def finish(self) -> HealthCheckResult:
        return HealthCheckResult(passed=True, consecutive_failures=0, polls=self.polls, reason="health gate passed")


def sample_from_outcomes(outcomes) -> HealthSample:
    """Summarise smoke-request outcomes (gateway ``Outcome``s) as one poll."""
    if not outcomes:
        return HealthSample(success_rate=0.0, p95_latency_ms=float("inf"), has_5xx_spike=True)
    ok = [o for o in outcomes if o.status is not None and o.status < 500]
    latencies = sorted(o.latency_ms for o in outcomes)
    p95 = latencies[max(0, int(round(0.95 * len(latencies))) - 1)]
    fives = sum(1 for o in outcomes if o.status is not None and o.status >= 500)
    return HealthSample(
        success_rate=len(ok) / len(outcomes),
        p95_latency_ms=p95,
        has_5xx_spike=fives > max(1, len(outcomes) // 10),
    )


# ---- fixed samplers: kept for the pure-gate tests ----
def healthy_sampler(_: int) -> HealthSample:
    return HealthSample(success_rate=0.999, p95_latency_ms=180.0, has_5xx_spike=False)


def failing_sampler(i: int) -> HealthSample:
    # first poll looks fine, then it degrades — realistic bad-deploy shape
    if i == 0:
        return HealthSample(success_rate=0.995, p95_latency_ms=220.0)
    return HealthSample(success_rate=0.80, p95_latency_ms=1200.0, has_5xx_spike=True)
