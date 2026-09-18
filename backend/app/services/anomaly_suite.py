"""The three-layer anomaly suite and its consensus gate.

Every control-loop tick, for every service, three independent named methods
look at the same readings:

    1. z-score            (anomaly_detector.ZScoreLayer)
    2. seasonal z-score   (seasonal_detector.SeasonalLayer, STL residual)
    3. Isolation Forest   (isolation_forest_detector.IsolationForestLayer)

    agreeing_layers >= 2  -> auto-act: open an incident; the service becomes
                             eligible for automated rollback
    agreeing_layers == 1  -> surfaced on the dashboard, no action
    agreeing_layers == 0  -> nothing

Requiring two independent methods to agree is how the suite keeps false
positives down — rather than raising any single method's threshold. A layer
still warming up cannot vote, so until two layers are warm, nothing
auto-acts; the dashboard shows each layer's warm-up progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AnomalyConsensus, Metric
from app.services.anomaly_detector import METRICS, LayerResult, ZScoreLayer
from app.services.isolation_forest_detector import IsolationForestLayer, feature_vector
from app.services.seasonal_detector import SeasonalLayer


def epoch(dt: datetime) -> float:
    return dt.replace(tzinfo=UTC).timestamp()


@dataclass
class SuiteResult:
    service: str
    at: datetime
    zscore: LayerResult
    seasonal: LayerResult
    iforest: LayerResult
    zscores: dict[str, Optional[float]] = field(default_factory=dict)
    residual_z: dict[str, Optional[float]] = field(default_factory=dict)
    iforest_score: Optional[float] = None
    vector: Optional[list[float]] = None
    min_layers: int = settings.consensus_min_layers

    @property
    def layers(self) -> list[LayerResult]:
        return [self.zscore, self.seasonal, self.iforest]

    @property
    def agreeing_layers(self) -> int:
        return sum(1 for layer in self.layers if layer.flagged)

    @property
    def auto_action(self) -> bool:
        return self.agreeing_layers >= self.min_layers

    def summary(self) -> dict:
        return {
            "zscore_flagged": self.zscore.flagged,
            "seasonal_flagged": self.seasonal.flagged,
            "isolation_forest_flagged": self.iforest.flagged,
            "agreeing_layers": self.agreeing_layers,
            "auto_action": self.auto_action,
            "layers": {layer.layer: layer.to_dict() for layer in self.layers},
        }

    def to_row(self) -> AnomalyConsensus:
        return AnomalyConsensus(
            service_name=self.service,
            window_end=self.at,
            zscore_flagged=self.zscore.flagged,
            seasonal_flagged=self.seasonal.flagged,
            isolation_forest_flagged=self.iforest.flagged,
            agreeing_layers=self.agreeing_layers,
            auto_action_taken=self.auto_action,
            details={layer.layer: layer.to_dict() for layer in self.layers},
        )


class AnomalySuite:
    def __init__(
        self,
        zscore: Optional[ZScoreLayer] = None,
        seasonal: Optional[SeasonalLayer] = None,
        iforest: Optional[IsolationForestLayer] = None,
        min_layers: int = settings.consensus_min_layers,
    ) -> None:
        self.zscore = zscore or ZScoreLayer()
        self.seasonal = seasonal or SeasonalLayer()
        self.iforest = iforest or IsolationForestLayer()
        self.min_layers = min_layers
        self._last_agreeing: dict[str, int] = {}

    def evaluate(
        self, service: str, values: dict[str, Optional[float]], errors: int, total: int, now: datetime
    ) -> SuiteResult:
        z_res, zs = self.zscore.observe(service, values)
        s_res, rz = self.seasonal.observe(service, values, epoch(now))
        vector = feature_vector(values, errors, total)
        i_res, score = self.iforest.observe(service, vector)
        return SuiteResult(service, now, z_res, s_res, i_res, zs, rz, score, vector, self.min_layers)

    def learn(self, result: SuiteResult, presumed_normal: bool) -> None:
        """Feed the Isolation Forest only readings nothing else doubts."""
        if presumed_normal:
            self.iforest.learn(result.service, result.vector)

    def should_record(self, result: SuiteResult) -> bool:
        """Consensus rows are written while any layer is flagged and on every
        change of the agreement count (so recoveries are recorded too)."""
        prev = self._last_agreeing.get(result.service)
        self._last_agreeing[result.service] = result.agreeing_layers
        return result.agreeing_layers > 0 or (prev is not None and prev != result.agreeing_layers)

    def seed_from_db(self, db: Session, services: list[str], now: datetime) -> None:
        """Warm the z-score windows and the seasonal history from stored
        readings, so a restart does not reset every baseline."""
        seasonal_span = timedelta(seconds=self.seasonal.bucket_s * (self.seasonal.min_buckets + self.seasonal.period))
        for service in services:
            for metric in METRICS:
                rows = db.execute(
                    select(Metric.recorded_at, Metric.value)
                    .where(
                        Metric.service_name == service,
                        Metric.metric_name == metric,
                        Metric.recorded_at >= now - seasonal_span,
                    )
                    .order_by(Metric.recorded_at)
                ).all()
                if not rows:
                    continue
                self.zscore.seed(service, metric, [r[1] for r in rows])
                self.seasonal.seed(service, metric, [(epoch(r[0]), r[1]) for r in rows])
