"""Rollback triggers + anti-flapping cooldown.

``evaluate`` is pure: everything it needs, including whether the service is
cooling down, is passed in. The cooldown itself is a database row, so these
tests also cover it surviving a restart.
"""

from datetime import datetime, timedelta

from app.config import settings
from app.models import RollbackCooldown
from app.services.rollback_engine import RollbackEngine, RollbackTrigger

T0 = datetime(2026, 9, 28, 12, 0, 0)


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
    eng = RollbackEngine()
    auto = eng.evaluate("svc", burn_severity="FAST_BURN", consensus_layers=3, in_cooldown=True)
    assert not auto.should_rollback and "cooldown" in auto.reason
    manual = eng.evaluate("svc", manual=True, in_cooldown=True)
    assert manual.should_rollback and manual.trigger == RollbackTrigger.MANUAL


def test_rollout_gates_are_not_suppressed_by_cooldown():
    """A candidate failing its own gate must be aborted even during cooldown,
    or it would be left half-rolled-out."""
    eng = RollbackEngine()
    assert eng.evaluate("svc", consecutive_health_failures=3, in_cooldown=True).should_rollback
    assert eng.evaluate("svc", canary_gate_failure="x", in_cooldown=True).should_rollback


def test_cooldown_is_persisted_and_outlives_the_process(db):
    eng = RollbackEngine()
    eng.start_cooldown(db, "svc", T0, trigger=RollbackTrigger.SLO_FAST_BURN)
    assert eng.cooldown_remaining(db, "svc", T0) == settings.rollback_cooldown_s
    assert db.get(RollbackCooldown, "svc").trigger == "slo_fast_burn"

    # A restarted backend, or a second worker, is a fresh engine over the same
    # database: it must see the same window.
    restarted = RollbackEngine()
    assert restarted.in_cooldown(db, "svc", T0 + timedelta(seconds=30))
    assert not restarted.in_cooldown(db, "svc", T0 + timedelta(seconds=settings.rollback_cooldown_s + 1))
    assert not restarted.in_cooldown(db, "other-svc", T0)


def test_anti_flapping_prevents_loop(db):
    """Rollback -> cooldown -> a fresh FAST_BURN must NOT auto-rollback again."""
    eng = RollbackEngine()
    first = eng.evaluate(
        "svc", burn_severity="FAST_BURN", consensus_layers=2, in_cooldown=eng.in_cooldown(db, "svc", T0)
    )
    assert first.should_rollback
    eng.start_cooldown(db, "svc", T0, trigger=first.trigger)

    later = T0 + timedelta(seconds=5)
    second = eng.evaluate(
        "svc", burn_severity="FAST_BURN", consensus_layers=2, in_cooldown=eng.in_cooldown(db, "svc", later)
    )
    assert not second.should_rollback  # suppressed -> loop broken

    after = T0 + timedelta(seconds=settings.rollback_cooldown_s + 1)
    third = eng.evaluate(
        "svc", burn_severity="FAST_BURN", consensus_layers=2, in_cooldown=eng.in_cooldown(db, "svc", after)
    )
    assert third.should_rollback  # the window closes on its own
