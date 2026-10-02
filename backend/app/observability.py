"""Prometheus metrics for the backend itself.

The control loop measures the services it drives; this module measures the
loop. Two kinds of series:

* **Counters and histograms** are moved where the event happens — a tick
  finishes, a rollback fires, a drill completes, a gate returns a verdict, a
  request is served.
* **Gauges are read from the live runtime at scrape time**
  (``RuntimeCollector``), never written on a timer. A gauge that is only
  refreshed by the control loop would keep reporting healthy numbers if the
  loop itself were wedged; collecting on scrape means the staleness shows up
  as ``opsforge_seconds_since_last_tick`` instead.

Scrape it at ``GET /metrics``. (``/metrics/{service}`` and
``/metrics/ingest`` are the V1 telemetry API, which is a different thing:
readings the backend stores, not readings about the backend.)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector

from app.clock import utcnow

CONTENT_TYPE = CONTENT_TYPE_LATEST

# --- the control loop ---------------------------------------------------
TICKS = Counter("opsforge_control_loop_ticks_total", "Control-loop ticks completed")
TICK_ERRORS = Counter("opsforge_control_loop_errors_total", "Control-loop ticks that raised")
TICK_SECONDS = Histogram(
    "opsforge_control_loop_tick_seconds",
    "Wall-clock duration of one control-loop tick",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
OPEN_INCIDENTS = Gauge("opsforge_open_incidents", "Incidents currently open")
ACTIVE_ROLLOUTS = Gauge("opsforge_active_rollouts", "Rollouts currently in flight")

# --- what the system decided --------------------------------------------
INCIDENTS_OPENED = Counter("opsforge_incidents_opened_total", "Incidents opened", ["severity", "cascading"])
ROLLBACKS = Counter("opsforge_rollbacks_total", "Rollbacks performed", ["trigger"])
ROLLOUTS_FINISHED = Counter(
    "opsforge_rollouts_finished_total", "Rollouts that reached a terminal state", ["strategy", "status"]
)
GATE_VERDICTS = Counter("opsforge_canary_gate_evaluations_total", "Canary stage gate evaluations", ["verdict"])
DRILLS = Counter("opsforge_chaos_drills_total", "Chaos drills completed", ["fault_type", "verdict"])
RCA_REPORTS = Counter("opsforge_rca_reports_total", "RCA reports produced", ["path"])

# --- the API ------------------------------------------------------------
HTTP_REQUESTS = Counter("opsforge_http_requests_total", "HTTP requests served", ["method", "route", "status"])
HTTP_SECONDS = Histogram(
    "opsforge_http_request_seconds",
    "HTTP request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


def observe_tick(seconds: float, failed: bool = False) -> None:
    TICKS.inc()
    TICK_SECONDS.observe(seconds)
    if failed:
        TICK_ERRORS.inc()


class RuntimeCollector(Collector):
    """Per-service gauges, read from the runtime when Prometheus scrapes."""

    def __init__(self, get_runtime) -> None:
        self._get_runtime = get_runtime

    def collect(self) -> Iterable[Any]:
        loop = GaugeMetricFamily("opsforge_control_loop_running", "1 while the control loop is running")
        serving = GaugeMetricFamily("opsforge_traffic_serving", "1 once every live colour is Ready")
        since = GaugeMetricFamily("opsforge_seconds_since_last_tick", "Age of the last completed tick")
        burn = GaugeMetricFamily(
            "opsforge_service_burn_rate", "SLO error-budget burn rate", labels=["service", "window"]
        )
        budget = GaugeMetricFamily(
            "opsforge_service_budget_remaining", "Fraction of the error budget left", labels=["service"]
        )
        flagged = GaugeMetricFamily(
            "opsforge_service_layers_flagged", "Detector layers flagging this service", labels=["service"]
        )
        warm = GaugeMetricFamily(
            "opsforge_service_layer_warm",
            "1 when a detector layer has enough history to vote",
            labels=["service", "layer"],
        )
        rps = GaugeMetricFamily(
            "opsforge_service_requests_per_second", "Edge requests measured in the last tick", labels=["service"]
        )

        rt = self._get_runtime()
        if rt is None:
            return [loop, serving, since, burn, budget, flagged, warm, rps]

        loop.add_metric([], 1 if rt.running else 0)
        serving.add_metric([], 1 if rt.serving else 0)
        if rt.last_tick_at is not None:
            since.add_metric([], max(0.0, (utcnow() - rt.last_tick_at).total_seconds()))
        for service, b in (rt.burns or {}).items():
            fast = next((e for e in b.evaluations if e.severity == "FAST_BURN"), None)
            if fast is not None:
                if fast.long_burn is not None:
                    burn.add_metric([service, "1h"], fast.long_burn)
                if fast.short_burn is not None:
                    burn.add_metric([service, "5m"], fast.short_burn)
            if b.budget_remaining is not None:
                budget.add_metric([service], b.budget_remaining)
        for service, result in (rt.results or {}).items():
            flagged.add_metric([service], result.agreeing_layers)
            for layer in result.layers:
                warm.add_metric([service, layer.layer], 1 if layer.warm else 0)
        for service, traffic in (rt.traffic or {}).items():
            if traffic and traffic.get("rps") is not None:
                rps.add_metric([service], traffic["rps"])
        return [loop, serving, since, burn, budget, flagged, warm, rps]


_collector: Optional[RuntimeCollector] = None


def register_runtime_collector(get_runtime) -> None:
    """Idempotent: uvicorn's reloader imports this module more than once."""
    global _collector
    if _collector is None:
        _collector = RuntimeCollector(get_runtime)
        REGISTRY.register(_collector)


def exposition() -> bytes:
    return generate_latest(REGISTRY)
