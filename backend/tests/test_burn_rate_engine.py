"""SLO burn-rate alerting (Google SRE Workbook ch.5) and the canary gate."""

from datetime import datetime, timedelta

import pytest

from app.models import BurnRateAlert, SLISample
from app.services.burn_rate_engine import (
    POLICY,
    BurnRateService,
    WindowCounts,
    budget_fraction,
    burn_rate,
    evaluate_canary_gate,
    evaluate_policy,
    wilson_lower_bound,
    worst_firing,
)

T0 = datetime(2026, 9, 18, 12, 0, 0)


def test_burn_rate_formula():
    # 1.44% bad against a 99.9% SLO spends budget 14.4x too fast
    assert burn_rate(144, 10_000, 0.999) == pytest.approx(14.4)
    assert burn_rate(0, 0, 0.999) is None
    with pytest.raises(ValueError):
        burn_rate(1, 10, 1.0)


def test_policy_matches_workbook_budget_consumption():
    fractions = {p.severity: budget_fraction(p, 30) for p in POLICY}
    assert fractions == pytest.approx({"FAST_BURN": 0.02, "MEDIUM_BURN": 0.05, "SLOW_BURN": 0.10})


def _series(rate_rps: float, bad_ratio_at):
    """counts_for() over a synthetic per-second stream; bad_ratio_at(t) gives
    the bad fraction at second t relative to T0 (t <= 0 is history)."""

    def factory(now: datetime):
        def counts_for(window: timedelta) -> WindowCounts:
            end = int((now - T0).total_seconds())
            start = end - int(window.total_seconds())
            total = bad = 0.0
            for t in range(start + 1, end + 1):
                total += rate_rps
                bad += rate_rps * bad_ratio_at(t)
            return WindowCounts(total=int(total), bad=int(round(bad)), first_at=now - window)

        return counts_for

    return factory


def test_fast_burn_detects_total_outage_in_about_52_seconds():
    """The workbook's detection time for a 100% outage at 14.4x over 1h is
    0.864 minutes: the 1h error ratio passes 1.44% after 1h*1.44% of downtime."""
    factory = _series(10.0, lambda t: 1.0 if t > 0 else 0.0)
    fired_at = None
    for second in range(1, 120):
        now = T0 + timedelta(seconds=second)
        fast = next(e for e in evaluate_policy(factory(now), 0.999, now) if e.severity == "FAST_BURN")
        if fast.firing:
            fired_at = second
            break
    assert fired_at is not None and 50 <= fired_at <= 54


def test_multiwindow_needs_both_windows():
    # A 1h window full of old errors, but the last 5 minutes are clean:
    # the incident is over, so the page must not (re)fire.
    counts = {timedelta(hours=1): WindowCounts(10_000, 500), timedelta(minutes=5): WindowCounts(1_000, 0)}
    evals = evaluate_policy(lambda w: counts.get(w, WindowCounts(10_000, 500)), 0.999, T0)
    assert not next(e for e in evals if e.severity == "FAST_BURN").firing

    # A 5-minute spike alone (1h ratio still low) must not page either.
    counts = {timedelta(hours=1): WindowCounts(36_000, 100), timedelta(minutes=5): WindowCounts(3_000, 100)}
    evals = evaluate_policy(lambda w: counts.get(w, WindowCounts(36_000, 100)), 0.999, T0)
    assert not next(e for e in evals if e.severity == "FAST_BURN").firing


def test_both_windows_over_threshold_fire_and_worst_wins():
    evals = evaluate_policy(lambda w: WindowCounts(10_000, 500), 0.999, T0)  # 5% bad -> 50x everywhere
    assert all(e.firing for e in evals)
    assert worst_firing(evals).severity == "FAST_BURN"


def test_short_window_needs_minimum_traffic():
    evals = evaluate_policy(lambda w: WindowCounts(5, 5), 0.999, T0, min_requests=20)
    assert not any(e.firing for e in evals)


# ------------------------------------------------------------- canary gate --
def test_wilson_bound_is_conservative_for_small_samples():
    assert wilson_lower_bound(1, 20) < 0.0144 < 1 / 20  # one bad of 20 proves nothing
    assert wilson_lower_bound(6, 20) > 0.0144


def _gate(canary, stable=None, elapsed=60.0, **kw):
    stable = stable or WindowCounts(400, 0)
    return evaluate_canary_gate(canary, stable, 0.999, elapsed_s=elapsed, window_s=45, min_requests=20, **kw)


def test_gate_passes_healthy_canary_after_window():
    v = _gate(WindowCounts(25, 0))
    assert v.verdict == "PASS"


def test_gate_does_not_convict_on_one_slow_request():
    v = _gate(WindowCounts(22, 1))
    assert v.verdict == "PASS" and v.canary_burn > 14.4  # point estimate is high, evidence is thin


def test_gate_fails_fast_on_confident_regression():
    v = _gate(WindowCounts(20, 8), elapsed=10.0)  # before the window ends
    assert v.verdict == "FAIL" and "95% confidence" in v.reason


def test_gate_waits_for_window_and_traffic():
    assert _gate(WindowCounts(30, 0), elapsed=10.0).verdict == "WAIT"
    assert _gate(WindowCounts(5, 0), elapsed=60.0).verdict == "WAIT"


def test_gate_fails_when_canary_never_gets_traffic():
    v = _gate(WindowCounts(3, 0), elapsed=400.0, max_wait_s=300)
    assert v.verdict == "FAIL" and "insufficient" in v.reason


def test_gate_notes_when_stable_is_also_burning():
    v = _gate(WindowCounts(40, 20), stable=WindowCounts(400, 200))
    assert v.verdict == "FAIL" and "not specific to the canary" in v.reason


# ------------------------------------------------------------- persistence --
def _add_samples(db, service, color, start, n_ticks, per_tick, bad_per_tick, tick_s=5):
    for i in range(n_ticks):
        end = start + timedelta(seconds=tick_s * (i + 1))
        db.add(
            SLISample(
                service_name=service,
                color=color,
                window_start=end - timedelta(seconds=tick_s),
                window_end=end,
                total_requests=per_tick,
                bad_requests=bad_per_tick,
                error_requests=bad_per_tick,
            )
        )
    db.commit()


def test_service_evaluation_and_alert_lifecycle(db):
    svc = BurnRateService()
    _add_samples(db, "payments-svc", "blue", T0, 120, 50, 0)  # 10 healthy minutes
    now = T0 + timedelta(minutes=10)
    burn = svc.evaluate(db, "payments-svc", now)
    assert burn.worst is None and burn.budget_remaining == pytest.approx(1.0)

    _add_samples(db, "payments-svc", "blue", now, 12, 50, 50)  # one minute of total failure
    now += timedelta(minutes=1)
    burn = svc.evaluate(db, "payments-svc", now)
    assert burn.worst == "FAST_BURN"
    fired = svc.record_transitions(db, burn, now)
    db.commit()
    assert {a.severity for a in fired} == {"FAST_BURN", "MEDIUM_BURN", "SLOW_BURN"}
    assert svc.record_transitions(db, burn, now) == []  # no duplicate while still firing

    _add_samples(db, "payments-svc", "blue", now, 72, 50, 0)  # six clean minutes: 5m window recovers
    now += timedelta(minutes=6)
    burn = svc.evaluate(db, "payments-svc", now)
    svc.record_transitions(db, burn, now)
    db.commit()
    fast = db.query(BurnRateAlert).filter_by(severity="FAST_BURN").one()
    assert fast.resolved_at == now


def test_canary_gate_reads_colour_specific_samples(db):
    svc = BurnRateService()
    _add_samples(db, "ledger-svc", "blue", T0, 12, 40, 0)
    _add_samples(db, "ledger-svc", "green", T0, 12, 4, 3)  # canary failing hard
    v = svc.gate(db, "ledger-svc", "green", "blue", T0, T0 + timedelta(seconds=60))
    assert v.verdict == "FAIL"
    assert v.canary_requests == 48 and v.stable_requests == 480
