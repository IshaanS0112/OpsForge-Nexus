"""Blue-green state machine + health gate."""

import pytest

from app.services.deployment_engine import (
    DeploymentStateMachine,
    DeployState,
    HealthChecker,
    HealthGate,
    HealthSample,
    IllegalTransition,
)

HEALTHY = HealthSample(success_rate=0.999, p95_latency_ms=180.0)
FAILING = HealthSample(success_rate=0.80, p95_latency_ms=1200.0, has_5xx_spike=True)


def _run(gate: HealthGate, samples):
    for sample in samples:
        result = gate.observe(sample)
        if result is not None:
            return result
    return None


def test_happy_path_transitions():
    sm = DeploymentStateMachine()
    path = [
        DeployState.PREPARING_GREEN,
        DeployState.DEPLOYING_GREEN,
        DeployState.HEALTH_CHECKING,
        DeployState.TRAFFIC_SWITCHING,
        DeployState.LIVE,
    ]
    for s in path:
        sm.transition(s)
    assert sm.state == DeployState.LIVE
    assert sm.history[0] == DeployState.IDLE


def test_illegal_transition_raises():
    sm = DeploymentStateMachine()
    with pytest.raises(IllegalTransition):
        sm.transition(DeployState.LIVE)  # cannot jump straight from IDLE


def test_rollback_branch_is_legal_from_health_checking():
    sm = DeploymentStateMachine()
    sm.transition(DeployState.PREPARING_GREEN)
    sm.transition(DeployState.DEPLOYING_GREEN)
    sm.transition(DeployState.HEALTH_CHECKING)
    sm.transition(DeployState.ROLLING_BACK)
    sm.transition(DeployState.ROLLED_BACK)
    assert sm.state == DeployState.ROLLED_BACK
    with pytest.raises(IllegalTransition):
        sm.transition(DeployState.LIVE)  # terminal


def test_health_gate_passes_after_its_full_window():
    checker = HealthChecker(interval_s=5, window_s=20)  # four polls
    gate = HealthGate(checker)
    assert [gate.observe(HEALTHY) for _ in range(3)] == [None, None, None]  # still polling
    result = gate.observe(HEALTHY)
    assert result.passed and result.polls == 4


def test_health_gate_fails_on_three_consecutive_failures():
    result = _run(HealthGate(HealthChecker()), [HEALTHY, FAILING, FAILING, FAILING])
    assert result.passed is False and result.consecutive_failures == 3
    assert result.reason == "3 consecutive failed health checks"


def test_a_good_poll_resets_the_failure_streak():
    gate = HealthGate(HealthChecker(interval_s=5, window_s=60))
    assert _run(gate, [FAILING, FAILING, HEALTHY, FAILING, FAILING]) is None
    assert gate.consecutive_failures == 2


def test_canary_smoke_check_fails_on_one_poll_and_says_so():
    result = HealthGate(HealthChecker.canary_smoke()).observe(HealthSample(success_rate=0.5, p95_latency_ms=100.0))
    assert result.passed is False
    assert result.reason == "smoke check failed: the candidate is not serving"


def test_poll_pass_criteria():
    hc = HealthChecker()
    assert hc.poll_passes(HealthSample(0.999, 180.0)) is True
    assert hc.poll_passes(HealthSample(0.95, 180.0)) is False  # success rate too low
    assert hc.poll_passes(HealthSample(0.999, 600.0)) is False  # latency too high
    assert hc.poll_passes(HealthSample(0.999, 180.0, has_5xx_spike=True)) is False
