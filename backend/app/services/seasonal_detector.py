"""Layer 2 of the anomaly suite: seasonal-adjusted z-score.

STL decomposition (Cleveland, Cleveland, McRae & Terpenning, 1990, "STL: A
Seasonal-Trend Decomposition Procedure Based on Loess"; statsmodels'
implementation) splits each metric's history into

    value = trend + seasonal + residual

and this layer z-scores the *residual* of each new reading — the part the
daily cycle and the slow trend do not explain — instead of the raw value.
That is the fix for the z-score layer's documented weakness: a traffic peak
that happens every day is expected here, not anomalous.

Mechanics:

* History is kept as wall-clock-aligned bucket means (``seasonal_bucket_s``);
  the season is ``seasonal_period_s`` long (86400 s in production; the demo
  compresses one "day" into 600 s, matching its load generator).
* STL is refitted every ``seasonal_refit_every_s`` (robust fitting, so past
  anomalies do not bend the seasonal shape).
* A reading's expectation is the latest trend level plus the seasonal value
  for its phase of the day.
* The residual scale is estimated robustly (MAD) from recent *per-tick*
  readings against the fitted trend + season, because live readings are
  single ticks, which are noisier than the bucket means STL was fitted on.
  It carries the same per-metric floors as layer 1, and the expectation is
  clamped at zero: these metrics cannot go negative, so a reading of 0.0
  must never count as worse than expected.
* Same rule as layer 1: residual z > 3.0 for K=3 consecutive readings,
  one-sided (all four metrics are "higher is worse").
* Cold start: nothing is judged until ``seasonal_min_periods`` full seasons
  of history exist — the same honest warm-up as the plain z-score.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.config import settings
from app.services.anomaly_detector import METRICS, SIGMA_FLOORS, AnomalyStreak, LayerResult


class BucketSeries:
    """Means over fixed wall-clock buckets (index = epoch // bucket_s)."""

    def __init__(self, bucket_s: float, max_buckets: int) -> None:
        self.bucket_s = bucket_s
        self.max_buckets = max_buckets
        self._b: OrderedDict[int, list[float]] = OrderedDict()  # index -> [sum, count]

    def index(self, t: float) -> int:
        return int(t // self.bucket_s)

    def add(self, t: float, value: float) -> None:
        i = self.index(t)
        cell = self._b.get(i)
        if cell is None:
            self._b[i] = [value, 1.0]
            while len(self._b) > self.max_buckets:
                self._b.popitem(last=False)
        else:
            cell[0] += value
            cell[1] += 1

    def complete_series(self, now: float) -> tuple[int, np.ndarray]:
        """Consecutive bucket means from the oldest bucket to the last
        completed one; gaps are linearly interpolated. -> (first index, values)"""
        current = self.index(now)
        keys = [k for k in self._b if k < current]
        if not keys:
            return current, np.array([])
        first, last = keys[0], keys[-1]
        idx = np.arange(first, last + 1)
        known = np.array(keys)
        vals = np.array([self._b[k][0] / self._b[k][1] for k in keys])
        return first, np.interp(idx, known, vals)

    def __len__(self) -> int:
        return len(self._b)


@dataclass
class SeasonalFit:
    first_index: int  # bucket index of trend[0]
    trend: np.ndarray
    seasonal: np.ndarray
    profile: np.ndarray  # seasonal value by phase (length = period in buckets)
    resid_center: float
    resid_scale: float

    @property
    def level(self) -> float:
        return float(self.trend[-1])

    def expected(self, bucket_index: int) -> float:
        return self.level + float(self.profile[bucket_index % len(self.profile)])

    def fitted(self, bucket_index: int) -> Optional[float]:
        """Trend + season at a bucket inside the fitted range."""
        i = bucket_index - self.first_index
        if 0 <= i < len(self.trend):
            return float(self.trend[i] + self.seasonal[i])
        return None


def robust_scale(residuals: np.ndarray) -> tuple[float, float]:
    """(median, 1.4826 * MAD): a sigma estimate that past anomalies cannot
    inflate the way they inflate a standard deviation."""
    center = float(np.median(residuals))
    return center, float(1.4826 * np.median(np.abs(residuals - center)))


def fit_stl(values: np.ndarray, first_index: int, period: int) -> SeasonalFit:
    """Pure: robust STL over bucket means, plus a phase-indexed profile of the
    last full season."""
    from statsmodels.tsa.seasonal import STL

    res = STL(values, period=period, robust=True).fit()
    trend, seasonal = np.asarray(res.trend), np.asarray(res.seasonal)
    profile = np.zeros(period)
    for i in range(len(values) - period, len(values)):
        profile[(first_index + i) % period] = seasonal[i]
    center, scale = robust_scale(np.asarray(res.resid))
    return SeasonalFit(first_index, trend, seasonal, profile, center, scale)


class SeasonalLayer:
    name = "seasonal"

    def __init__(
        self,
        period_s: float = settings.seasonal_period_s,
        bucket_s: float = settings.seasonal_bucket_s,
        min_periods: int = settings.seasonal_min_periods,
        refit_every_s: float = settings.seasonal_refit_every_s,
        threshold: float = settings.anomaly_z_threshold,
        k: int = settings.anomaly_consecutive_to_incident,
    ) -> None:
        self.bucket_s = bucket_s
        self.period = max(2, int(round(period_s / bucket_s)))
        self.min_buckets = self.period * min_periods
        self.refit_every_s = refit_every_s
        self.threshold = threshold
        self.streak = AnomalyStreak(z_threshold=threshold, consecutive_needed=k)
        self._series: dict[tuple[str, str], BucketSeries] = {}
        self._ticks: dict[tuple[str, str], deque] = {}
        self._fits: dict[tuple[str, str], SeasonalFit] = {}
        self._fitted_at: dict[tuple[str, str], float] = {}

    def _get(self, key: tuple[str, str]) -> BucketSeries:
        s = self._series.get(key)
        if s is None:
            s = self._series[key] = BucketSeries(self.bucket_s, self.min_buckets + self.period)
            self._ticks[key] = deque(maxlen=4000)
        return s

    def seed(self, service: str, metric: str, points: list[tuple[float, float]]) -> None:
        """Warm from stored history: (epoch seconds, value) oldest first."""
        s = self._get((service, metric))
        ticks = self._ticks[(service, metric)]
        for t, v in points:
            s.add(t, v)
            ticks.append((t, v))

    def warmup(self, service: str, metric: str, now: float) -> tuple[int, int]:
        s = self._series.get((service, metric))
        have = len(s.complete_series(now)[1]) if s else 0
        return have, self.min_buckets

    def _maybe_refit(self, key: tuple[str, str], now: float) -> Optional[SeasonalFit]:
        fit = self._fits.get(key)
        if fit is not None and now - self._fitted_at.get(key, 0.0) < self.refit_every_s:
            return fit
        first, values = self._series[key].complete_series(now)
        if len(values) < self.min_buckets:
            return fit
        fit = fit_stl(values, first, self.period)
        # Residual scale from per-tick readings against the fitted curve.
        resid = [v - f for t, v in self._ticks[key] if (f := fit.fitted(int(t // self.bucket_s))) is not None]
        if len(resid) >= 30:
            fit.resid_center, fit.resid_scale = robust_scale(np.asarray(resid))
        self._fits[key] = fit
        self._fitted_at[key] = now
        return fit

    def observe(
        self, service: str, values: dict[str, Optional[float]], now: float
    ) -> tuple[LayerResult, dict[str, Optional[float]]]:
        rz: dict[str, Optional[float]] = {}
        expected: dict[str, float] = {}
        warmup: dict[str, str] = {}
        streaks = {}
        flagged = warm = False
        for metric in METRICS:
            value = values.get(metric)
            if value is None:
                rz[metric] = None
                continue
            key = (service, metric)
            series = self._get(key)
            fit = self._maybe_refit(key, now)
            if fit is None:
                have, need = len(series.complete_series(now)[1]), self.min_buckets
                warmup[metric] = f"{have}/{need} buckets"
                z = None
            else:
                # None of these metrics can go below zero, so neither can their
                # expectation: without the clamp a past spike leaves a negative
                # swing behind and a healthy 0.0 reads as worse than expected.
                exp = max(0.0, fit.expected(series.index(now)) + fit.resid_center)
                absolute, relative = SIGMA_FLOORS.get(metric, (1e-9, 0.0))
                scale = max(fit.resid_scale, absolute, relative * abs(fit.level))
                z = (value - exp) / scale
                expected[metric] = round(exp, 4)
                warm = True
            rz[metric] = z
            upward = max(z, 0.0) if z is not None else None
            flagged = self.streak.observe(service, metric, upward) or flagged
            streaks[metric] = self.streak.count(service, metric)
            series.add(now, value)
            self._ticks[key].append((now, value))
        details = {
            "residual_z": {m: round(z, 2) for m, z in rz.items() if z is not None},
            "expected": expected,
            "streak": streaks,
            "period_s": self.period * self.bucket_s,
        }
        if warmup:
            details["warmup"] = warmup
        return LayerResult(self.name, flagged, warm, details), rz
