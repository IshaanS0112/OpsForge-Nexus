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


def test_error_rate_needs_sustain_window():
    eng = RollbackEngine()
    # above threshold but not sustained long enough -> no rollback
    d = eng.evaluate("svc", error_rate=0.09, error_rate_sustained_s=5)
    assert not d.should_rollback
    # sustained -> rollback
    d2 = eng.evaluate("svc", error_rate=0.09, error_rate_sustained_s=40)
    assert d2.should_rollback
    assert d2.trigger == RollbackTrigger.ERROR_RATE


def test_latency_trigger_relative_to_baseline():
    eng = RollbackEngine()
    d = eng.evaluate(
        "svc", latency_p95=1200, latency_baseline=250, latency_sustained_s=90
    )
    assert d.should_rollback
    assert d.trigger == RollbackTrigger.LATENCY_P95


def test_manual_bypasses_cooldown():
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    eng.start_cooldown("svc")
    # auto is suppressed during cooldown
    auto = eng.evaluate("svc", consecutive_health_failures=3)
    assert not auto.should_rollback
    # manual still fires
    manual = eng.evaluate("svc", manual=True)
    assert manual.should_rollback
    assert manual.trigger == RollbackTrigger.MANUAL


def test_cooldown_expires():
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    eng.start_cooldown("svc")
    assert eng.in_cooldown("svc")
    clock.advance(settings.rollback_cooldown_s + 1)
    assert not eng.in_cooldown("svc")
    # after cooldown, auto-rollback fires again
    d = eng.evaluate("svc", consecutive_health_failures=3)
    assert d.should_rollback


def test_anti_flapping_prevents_loop():
    """Rollback -> cooldown -> a fresh anomaly must NOT auto-rollback again."""
    clock = FakeClock()
    eng = RollbackEngine(clock=clock)
    first = eng.evaluate("svc", consecutive_health_failures=3)
    assert first.should_rollback
    eng.start_cooldown("svc")
    second = eng.evaluate("svc", consecutive_health_failures=3)
    assert not second.should_rollback  # suppressed -> loop broken
