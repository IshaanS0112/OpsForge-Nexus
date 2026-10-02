"""The three anomaly layers and their consensus gate, tested on the claims
each layer exists to make good on."""

import math
import random
from datetime import datetime, timedelta

import numpy as np

from app.services.anomaly_detector import ZScoreLayer, floored_zscore
from app.services.anomaly_suite import AnomalySuite
from app.services.isolation_forest_detector import IsolationForestLayer, feature_vector
from app.services.seasonal_detector import SeasonalLayer, fit_stl

PERIOD_S, TICK_S = 600, 5
T0 = 1_790_000_000.0  # epoch seconds, aligned to nothing in particular


def diurnal(t: float, rng: random.Random) -> float:
    """Latency with a daily (here: 10-minute) cycle and a scheduled batch job:
    every "day" at the same time, 30 seconds of load that adds 50 ms."""
    phase = (t % PERIOD_S) / PERIOD_S
    batch = 50.0 if 0.20 <= phase < 0.25 else 0.0
    return 100 + 20 * math.sin(2 * math.pi * phase) + batch + rng.gauss(0, 3)


def test_zscore_variance_floor_flags_jump_off_flat_baseline():
    window = [0.0] * 50  # a service that has never errored
    assert floored_zscore(0.3, window, "error_rate") > 3  # V1's zscore() returned None here


def test_zscore_layer_is_one_sided_and_keeps_baseline_clean():
    layer = ZScoreLayer(window=100, threshold=3.0, k=3)
    rng = random.Random(1)
    for _ in range(60):
        layer.observe("s", {"latency_p95": 100 + rng.gauss(0, 2)})
    # a drop is not a problem for a "higher is worse" metric
    for _ in range(5):
        res, _ = layer.observe("s", {"latency_p95": 20.0})
    assert not res.flagged
    for _ in range(3):
        res, zs = layer.observe("s", {"latency_p95": 400.0})
    assert res.flagged
    # after a long anomaly the z-score is still high: the spike never entered the window
    for _ in range(20):
        res, zs = layer.observe("s", {"latency_p95": 400.0})
    assert zs["latency_p95"] > 50 and res.flagged


def test_seasonal_layer_expects_the_recurring_batch_job_that_fools_plain_zscore():
    rng = random.Random(7)
    seasonal = SeasonalLayer(period_s=PERIOD_S, bucket_s=TICK_S, min_periods=2, refit_every_s=60)
    zlayer = ZScoreLayer(window=100, threshold=3.0, k=3)
    t = T0
    for _ in range(int(3 * PERIOD_S / TICK_S)):  # three clean "days" of history
        v = diurnal(t, rng)
        seasonal.observe("s", {"latency_p95": v}, t)
        zlayer.observe("s", {"latency_p95": v})
        t += TICK_S

    z_flags = s_flags = 0
    for _ in range(int(PERIOD_S / TICK_S)):  # one more normal day
        v = diurnal(t, rng)
        s_res, _ = seasonal.observe("s", {"latency_p95": v}, t)
        z_res, _ = zlayer.observe("s", {"latency_p95": v})
        z_flags += z_res.flagged
        s_flags += s_res.flagged
        t += TICK_S
    assert z_flags > 0  # the plain z-score pages on the batch job, every day
    assert s_flags == 0  # the seasonal residual knows it runs at this time

    # ...and a genuine anomaly still stands out from the seasonal expectation
    for _ in range(3):
        s_res, rz = seasonal.observe("s", {"latency_p95": diurnal(t, rng) + 60}, t)
        t += TICK_S
    assert s_res.flagged and rz["latency_p95"] > 3


def test_seasonal_layer_never_flags_a_zero_reading_after_a_past_spike():
    # A burst leaves a negative swing in the fitted season. A healthy service
    # reporting 0.0 errors must not read as "worse than expected".
    layer = SeasonalLayer(period_s=PERIOD_S, bucket_s=TICK_S, min_periods=2, refit_every_s=TICK_S)
    t = T0
    for _ in range(int(3 * PERIOD_S / TICK_S)):
        one_off = 0.5 if 2 * PERIOD_S <= t - T0 < 2 * PERIOD_S + 60 else 0.0  # a drill, once
        layer.observe("s", {"error_rate": one_off}, t)
        t += TICK_S
    flags = []
    for _ in range(int(PERIOD_S / TICK_S)):  # a clean cycle: no errors at all
        res, rz = layer.observe("s", {"error_rate": 0.0}, t)
        flags.append(res.flagged)
        assert (rz["error_rate"] or 0) <= 0  # zero can only be at or below expectation
        t += TICK_S
    assert not any(flags)


def test_seasonal_layer_warms_up_honestly():
    layer = SeasonalLayer(period_s=PERIOD_S, bucket_s=TICK_S, min_periods=2)
    res, rz = layer.observe("s", {"latency_p95": 100.0}, T0)
    assert not res.warm and rz["latency_p95"] is None
    assert "warmup" in res.details


def test_stl_recovers_the_seasonal_shape():
    t = np.arange(360)
    values = 50 + 10 * np.sin(2 * np.pi * t / 60) + np.random.default_rng(0).normal(0, 0.5, 360)
    fit = fit_stl(values, first_index=0, period=60)
    peak_phase = int(np.argmax(fit.profile))
    assert abs(peak_phase - 15) <= 2  # sin peaks a quarter period in
    assert fit.resid_scale < 1.0


def test_isolation_forest_catches_joint_drift_no_single_zscore_sees():
    rng = np.random.default_rng(3)
    means = np.array([0.02, 120.0, 0.30, 0.40])
    stds = np.array([0.004, 8.0, 0.03, 0.02])
    forest = IsolationForestLayer(train_window=500, min_train=100, retrain_every=10_000, trees=200, k=3, seed=1)
    zlayer = ZScoreLayer(window=100, threshold=3.0, k=3)
    names = ("error_rate", "latency_p95", "cpu_usage", "memory_usage")
    for _ in range(400):
        x = means + stds * rng.normal(size=4)
        forest.learn("s", list(x))
        zlayer.observe("s", dict(zip(names, x, strict=True)))

    drifted = means + 2.4 * stds  # every metric 2.4 sigma high at once
    for _ in range(3):
        z_res, zs = zlayer.observe("s", dict(zip(names, drifted, strict=True)))
        f_res, score = forest.observe("s", list(drifted))
    assert all(abs(z) < 3 for z in zs.values())  # each metric alone looks fine
    assert not z_res.flagged
    assert f_res.flagged and score < f_res.details["threshold"]


def test_isolation_forest_waits_for_a_clean_baseline():
    forest = IsolationForestLayer(min_train=30)
    res, score = forest.observe("s", [0.02, 100, 0.3, 0.4])
    assert not res.warm and score is None and "0/30" in res.details["training"]


def test_error_ratio_feature_is_smoothed():
    assert feature_vector({"latency_p95": 1, "cpu_usage": 1, "memory_usage": 1}, 0, 48) == [1 / 50, 1, 1, 1]
    assert feature_vector({"latency_p95": 1, "cpu_usage": None, "memory_usage": 1}, 0, 48) is None


def test_consensus_needs_two_layers():
    suite = AnomalySuite(
        seasonal=SeasonalLayer(period_s=60, bucket_s=5, min_periods=2, refit_every_s=30),
        iforest=IsolationForestLayer(min_train=20, retrain_every=20, trees=50),
    )
    rng = random.Random(2)
    now = datetime(2026, 9, 18, 12, 0, 0)

    def reading(err: float) -> dict:
        return {
            "error_rate": err,
            "latency_p95": 20 + rng.gauss(0, 1),
            "cpu_usage": 0.2 + rng.gauss(0, 0.01),
            "memory_usage": 0.3,
        }

    for _ in range(40):  # warm: z-score (20), IF (20), seasonal (two 60 s periods)
        r = suite.evaluate("s", reading(0.0), 0, 50, now)
        suite.learn(r, presumed_normal=not r.auto_action)
        now += timedelta(seconds=5)
    assert r.agreeing_layers == 0 and not r.auto_action
    assert all(layer.warm for layer in (r.zscore, r.seasonal, r.iforest))

    for _ in range(3):
        r = suite.evaluate("s", reading(0.4), 20, 50, now)
        now += timedelta(seconds=5)
    assert r.zscore.flagged and r.seasonal.flagged
    assert r.agreeing_layers >= 2 and r.auto_action
    row = r.to_row()
    assert row.auto_action_taken and row.agreeing_layers == r.agreeing_layers


def test_consensus_rows_written_on_change_only():
    suite = AnomalySuite()

    class R:
        def __init__(self, service, n):
            self.service, self.agreeing_layers = service, n

    assert not suite.should_record(R("s", 0))
    assert suite.should_record(R("s", 1))
    assert suite.should_record(R("s", 1))  # still flagged: keep recording
    assert suite.should_record(R("s", 0))  # the recovery is recorded
    assert not suite.should_record(R("s", 0))
