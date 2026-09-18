"""Layer 3 of the anomaly suite: Isolation Forest (Liu, Ting & Zhou, 2008).

Unsupervised and per service. It learns what "normal" looks like jointly
across four features and flags readings that are easy to isolate from that
cloud — which catches the failure mode neither z-score variant can: several
metrics drifting together while none crosses its own 3-sigma line.

    features: error ratio, latency p95, cpu usage, memory usage

Why unsupervised: there are no labelled incidents to train on, so the model
makes no accuracy claim it could not back. It needs a clean baseline first
(same cold start as the z-score): it trains only on "presumed normal" ticks —
no detector consensus, no open incident, no chaos fault running — and does not
score anything until ``iforest_min_train`` of them exist.

Threshold: sklearn's ``score_samples`` (lower = more anomalous). A reading is
anomalous when it scores below the ``iforest_contamination`` quantile of the
model's own training scores; K=3 consecutive, like the other layers.

Error ratio feature: add-one smoothed, (errors + 1) / (requests + 2). A
service that never errors during training would otherwise give the forest a
constant column, which isolation trees cannot split on, leaving it blind to
an error burst. The smoothed ratio still varies with request volume, so the
feature has a real range and a burst lands far outside it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.config import settings
from app.services.anomaly_detector import AnomalyStreak, LayerResult

FEATURES = ("error_ratio", "latency_p95", "cpu_usage", "memory_usage")


def feature_vector(values: dict[str, Optional[float]], errors: int, total: int) -> Optional[list[float]]:
    if total <= 0:
        return None
    rest = [values.get(m) for m in FEATURES[1:]]
    if any(v is None for v in rest):
        return None
    return [(errors + 1) / (total + 2), *rest]


@dataclass
class TrainedForest:
    model: object
    threshold: float
    trained_on: int


class IsolationForestLayer:
    name = "isolation_forest"

    def __init__(
        self,
        train_window: int = settings.iforest_train_window,
        min_train: int = settings.iforest_min_train,
        retrain_every: int = settings.iforest_retrain_every,
        contamination: float = settings.iforest_contamination,
        trees: int = settings.iforest_trees,
        k: int = settings.anomaly_consecutive_to_incident,
        seed: int = 42,
    ) -> None:
        self.train_window = train_window
        self.min_train = min_train
        self.retrain_every = retrain_every
        self.contamination = contamination
        self.trees = trees
        self.seed = seed
        self.streak = AnomalyStreak(z_threshold=0.5, consecutive_needed=k)  # fed 1.0 / 0.0
        self._train: dict[str, deque] = {}
        self._since_fit: dict[str, int] = {}
        self._models: dict[str, TrainedForest] = {}

    def fit(self, rows: list[list[float]]) -> TrainedForest:
        from sklearn.ensemble import IsolationForest

        X = np.asarray(rows, dtype=float)
        model = IsolationForest(n_estimators=self.trees, random_state=self.seed)
        model.fit(X)
        threshold = float(np.quantile(model.score_samples(X), self.contamination))
        return TrainedForest(model, threshold, len(rows))

    def learn(self, service: str, vector: Optional[list[float]]) -> None:
        """Add a presumed-normal reading; (re)train when due."""
        if vector is None:
            return
        buf = self._train.setdefault(service, deque(maxlen=self.train_window))
        buf.append(vector)
        self._since_fit[service] = self._since_fit.get(service, 0) + 1
        due = service not in self._models or self._since_fit[service] >= self.retrain_every
        if len(buf) >= self.min_train and due:
            self._models[service] = self.fit(list(buf))
            self._since_fit[service] = 0

    def training_size(self, service: str) -> int:
        return len(self._train.get(service, ()))

    def observe(self, service: str, vector: Optional[list[float]]) -> tuple[LayerResult, Optional[float]]:
        forest = self._models.get(service)
        if forest is None or vector is None:
            self.streak.observe(service, "joint", None)
            details = {"training": f"{self.training_size(service)}/{self.min_train} normal readings"}
            return LayerResult(self.name, False, False, details), None
        score = float(forest.model.score_samples(np.asarray([vector], dtype=float))[0])
        below = score < forest.threshold
        flagged = self.streak.observe(service, "joint", 1.0 if below else 0.0)
        details = {
            "score": round(score, 4),
            "threshold": round(forest.threshold, 4),
            "streak": self.streak.count(service, "joint"),
            "trained_on": forest.trained_on,
            "features": dict(zip(FEATURES, [round(v, 4) for v in vector], strict=True)),
        }
        return LayerResult(self.name, flagged, True, details), score
