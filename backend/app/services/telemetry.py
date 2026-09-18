"""Per-tick telemetry: resource scraping and persistence.

Where each metric comes from:

* ``error_rate``, ``latency_p95`` — the gateway's measurements of real edge
  requests this tick (``TrafficRecorder``).
* ``cpu_usage``, ``memory_usage`` — each serving pod's Prometheus-format
  ``/metrics`` (``process_cpu_seconds_total``, ``process_resident_memory_bytes``),
  scraped through the API server's pod proxy and normalised against the
  container limits: CPU as the rate of CPU-seconds over the scrape interval
  divided by the core limit, memory as RSS over the memory limit.

Persisted per tick: one ``metrics`` row per metric (with each detector
layer's output alongside the value), one ``sli_samples`` row per colour that
served traffic (the counts burn rates are summed from), and one ``logs`` row
per distinct error signature with its occurrence count.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.k8s import manifests as m
from app.models import Log, Metric, SLISample
from app.services.gateway import ServiceWindow
from app.services.k8s_client import PodInfo, parse_prometheus_text

logger = logging.getLogger(__name__)


class Telemetry:
    def __init__(self, cluster, max_workers: int = 8) -> None:
        self.cluster = cluster
        self._cpu_prev: dict[str, tuple[float, float]] = {}  # pod -> (epoch, cpu seconds)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scrape")

    def _scrape_pod(self, pod: PodInfo) -> Optional[dict[str, float]]:
        code, text = self.cluster.pod_request(pod.name, "GET", "metrics", timeout=2.0)
        if code != 200:
            return None
        return parse_prometheus_text(text)

    def scrape(self, colors_by_service: dict[str, list[str]], now_epoch: float) -> dict[str, dict[str, float]]:
        """Mean CPU and memory utilisation across each service's Ready pods
        of the given colours."""
        targets: list[tuple[str, PodInfo]] = []
        for service, colors in colors_by_service.items():
            try:
                pods = self.cluster.list_pods(service)
            except Exception as exc:  # noqa: BLE001 — a scrape miss is a gap, not a failure
                logger.warning("list pods for %s failed: %s", service, exc)
                continue
            targets += [(service, p) for p in pods if p.ready and p.color in colors and not p.terminating]
        results = list(self._pool.map(lambda sp: (sp[0], sp[1], self._safe_scrape(sp[1])), targets))

        per_service: dict[str, dict[str, list[float]]] = {}
        live_pods = set()
        for service, pod, sample in results:
            if sample is None:
                continue
            live_pods.add(pod.name)
            acc = per_service.setdefault(service, {"cpu_usage": [], "memory_usage": []})
            rss = sample.get("process_resident_memory_bytes")
            if rss is not None:
                acc["memory_usage"].append(rss / m.MEMORY_LIMIT_BYTES)
            cpu = sample.get("process_cpu_seconds_total")
            if cpu is not None:
                prev = self._cpu_prev.get(pod.name)
                self._cpu_prev[pod.name] = (now_epoch, cpu)
                if prev is not None and now_epoch > prev[0] and cpu >= prev[1]:
                    acc["cpu_usage"].append((cpu - prev[1]) / (now_epoch - prev[0]) / m.CPU_LIMIT_CORES)
        for gone in set(self._cpu_prev) - live_pods:
            self._cpu_prev.pop(gone, None)
        return {s: {k: sum(v) / len(v) for k, v in acc.items() if v} for s, acc in per_service.items()}

    def _safe_scrape(self, pod: PodInfo) -> Optional[dict[str, float]]:
        try:
            return self._scrape_pod(pod)
        except Exception as exc:  # noqa: BLE001
            logger.debug("scrape %s failed: %s", pod.name, exc)
            return None

    @staticmethod
    def persist(
        db: Session,
        now: datetime,
        tick_s: float,
        windows: dict[str, ServiceWindow],
        values: dict[str, dict[str, Optional[float]]],
        layer_outputs: dict[str, dict],
    ) -> None:
        start = now - timedelta(seconds=tick_s)
        for service, vals in values.items():
            out = layer_outputs.get(service, {})
            zs, rz, score = out.get("zscores", {}), out.get("residual_z", {}), out.get("iforest_score")
            for metric, value in vals.items():
                if value is None:
                    continue
                db.add(
                    Metric(
                        service_name=service,
                        metric_name=metric,
                        value=value,
                        recorded_at=now,
                        z_score=zs.get(metric),
                        seasonal_residual_z=rz.get(metric),
                        isolation_forest_score=score,
                    )
                )
        for service, w in windows.items():
            for color, c in w.colors.items():
                db.add(
                    SLISample(
                        service_name=service,
                        color=None if color == "unknown" else color,
                        window_start=start,
                        window_end=now,
                        total_requests=c.total,
                        bad_requests=c.bad,
                        error_requests=c.errors,
                    )
                )
            for (signature, root), count in w.signatures().items():
                message = f"{signature} x{count}" + (f" (caused by {root})" if root else "")
                db.add(
                    Log(
                        service_name=service,
                        level="ERROR",
                        message=message,
                        error_signature=signature[:200],
                        root_signature=root[:200] if root else None,
                        logged_at=now,
                        occurrences=count,
                    )
                )
