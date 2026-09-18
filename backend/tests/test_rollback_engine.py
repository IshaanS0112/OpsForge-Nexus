"""Rollback triggers + anti-flapping cooldown."""

from app.config import settings
from app.services.rollback_engine import RollbackEngine, RollbackTrigger


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_consecutive_health_failures_trigger():
    eng = RollbackEngine()
    d = eng.evaluate("svc", consecutive_health_failures=3)
    assert d.should_rollback
    assert d.trigger == RollbackTrigger.CONSECUTIVE_HEALTH_FAILURES


def test_rollout_and_canary_gate_failures_trigger():
    eng = RollbackEngine()
    assert eng.evaluate("svc", rollout_failure="ImagePullBackOff").trigger == RollbackTrigger.ROLLOUT_FAILED
    d = eng.evaluate("svc", canary_gate_failure="burn 40x > 14.4x")
    assert d.should_rollback and d.trigger == RollbackTrigger.CANARY_GATE_FAILED


def test_fast_burn_with_consensus_triggers():
    eng = RollbackEngine()
    d = eng.evaluate("svc", burn_severity="FAST_BURN", burn_rate=31.0, consensus_layers=2)
    assert d.should_rollback
    assert d.trigger == RollbackTrigger.SLO_FAST_BURN
    assert "31.0x" in d.reason


def test_fast_burn_alone_only_pages():
    eng = RollbackEngine()
    d = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=1)
    assert not d.should_rollback and "consensus" in d.reason


def test_slower_burns_never_roll_back():
    eng = RollbackEngine()
    for sev in ("MEDIUM_BURN", "SLOW_BURN", None):
        assert not eng.evaluate("svc", burn_severity=sev, consensus_layers=3).should_rollback


def test_cascade_victim_is_not_rolled_back():
    eng = RollbackEngine()
    d = eng.evaluate("checkout-api", burn_severity="FAST_BURN", consensus_layers=3, cascade_root="payments-svc")
    assert not d.should_rollback and "payments-svc" in d.reason


def test_manual_bypasses_cooldown():
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    eng.start_cooldown("svc")
    # metric-triggered rollback is suppressed during cooldown
    auto = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=3)
    assert not auto.should_rollback
    # manual still fires
    manual = eng.evaluate("svc", manual=True)
    assert manual.should_rollback
    assert manual.trigger == RollbackTrigger.MANUAL


def test_rollout_gates_are_not_suppressed_by_cooldown():
    """A candidate failing its own gate must be aborted even during cooldown,
    or it would be left half-rolled-out."""
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    eng.start_cooldown("svc")
    assert eng.evaluate("svc", consecutive_health_failures=3).should_rollback
    assert eng.evaluate("svc", canary_gate_failure="x").should_rollback


def test_cooldown_expires():
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    eng.start_cooldown("svc")
    assert eng.in_cooldown("svc")
    clock.advance(settings.rollback_cooldown_s + 1)
    assert not eng.in_cooldown("svc")
    # after cooldown, metric-triggered rollback fires again
    d = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=2)
    assert d.should_rollback


def test_anti_flapping_prevents_loop():
    """Rollback -> cooldown -> a fresh FAST_BURN must NOT auto-rollback again."""
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    first = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=2)
    assert first.should_rollback
    eng.start_cooldown("svc")
    second = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=2)
    assert not second.should_rollback  # suppressed -> loop broken
