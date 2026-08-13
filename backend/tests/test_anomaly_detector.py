"""Z-score anomaly detection: pure function + streak logic."""
from app.services.anomaly_detector import AnomalyStreak, zscore


def test_zscore_none_when_warming_up():
    assert zscore(5.0, []) is None
    assert zscore(5.0, [1.0]) is None


def test_zscore_none_on_zero_variance():
    assert zscore(5.0, [3.0, 3.0, 3.0]) is None


def test_zscore_flags_outlier():
    window = [0.01] * 50  # stable low baseline
    window += [0.011, 0.009, 0.012]  # tiny noise so variance > 0
    z = zscore(0.5, window)
    assert z is not None and z > 3.0


def test_streak_requires_k_consecutive():
    streak = AnomalyStreak(z_threshold=3.0, consecutive_needed=3)
    assert streak.observe("s", "error_rate", 4.0) is False  # 1
    assert streak.observe("s", "error_rate", 4.0) is False  # 2
    assert streak.observe("s", "error_rate", 4.0) is True   # 3 -> incident


def test_streak_resets_on_normal_reading():
    streak = AnomalyStreak(z_threshold=3.0, consecutive_needed=3)
    streak.observe("s", "error_rate", 4.0)
    streak.observe("s", "error_rate", 4.0)
    streak.observe("s", "error_rate", 0.5)  # normal -> reset
    assert streak.observe("s", "error_rate", 4.0) is False  # back to 1


def test_streak_is_per_metric():
    streak = AnomalyStreak(z_threshold=3.0, consecutive_needed=2)
    assert streak.observe("s", "error_rate", 4.0) is False
    assert streak.observe("s", "latency_p95", 4.0) is False  # different metric
    assert streak.observe("s", "error_rate", 4.0) is True
