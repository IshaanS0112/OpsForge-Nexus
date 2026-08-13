"""Blue-green state machine + health gate."""
import pytest

from app.services.deployment_engine import (
    DeploymentStateMachine,
    DeployState,
    HealthChecker,
    HealthSample,
    IllegalTransition,
    failing_sampler,
    healthy_sampler,
)


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


def test_health_gate_passes_when_healthy():
    result = HealthChecker().run(healthy_sampler)
    assert result.passed is True
    assert result.consecutive_failures == 0


def test_health_gate_fails_on_three_consecutive():
    result = HealthChecker().run(failing_sampler)
    assert result.passed is False
    assert result.consecutive_failures >= 3


def test_health_gate_sleeps_between_polls(monkeypatch):
    """Proves the gate is genuinely time-based: it sleeps interval_s between
    each poll over the window (not a single instant check)."""
    from app.config import settings

    monkeypatch.setattr(settings, "health_check_fast", False)
    calls: list[float] = []
    hc = HealthChecker(interval_s=5, window_s=20)  # -> 4 polls, 3 gaps
    result = hc.run(healthy_sampler, sleep=lambda s: calls.append(s))
    assert result.passed is True
    assert calls == [5, 5, 5]  # slept between every consecutive pair of polls


def test_poll_pass_criteria():
    hc = HealthChecker()
    assert hc.poll_passes(HealthSample(0.999, 180.0)) is True
    assert hc.poll_passes(HealthSample(0.95, 180.0)) is False   # success rate too low
    assert hc.poll_passes(HealthSample(0.999, 600.0)) is False  # latency too high
    assert hc.poll_passes(HealthSample(0.999, 180.0, has_5xx_spike=True)) is False
