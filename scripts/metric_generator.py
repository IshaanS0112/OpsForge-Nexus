#!/usr/bin/env python3
"""Bring-your-own telemetry: feed an external service's metrics to the suite.

The control loop measures the catalog services itself. This script plays a
service the loop does not know about and posts one four-metric reading at a
time to ``/metrics/ingest-batch``, which runs it through the same three
detector layers and consensus gate as measured traffic:

1. a stable baseline, long enough for the z-score layer (20 readings) and the
   Isolation Forest (60 readings) to learn what normal looks like;
2. with ``--inject``, a burst of readings far outside that baseline. Two
   agreeing layers open an incident; one layer alone never does.

The seasonal layer needs two full periods of history, so a short run is
judged by the other two layers only.

Usage:
    python3 scripts/metric_generator.py --service external-api
    python3 scripts/metric_generator.py --service external-api --inject
"""

from __future__ import annotations

import argparse
import random
import time

import _api

BASELINE = {  # (mean, stddev)
    "error_rate": (0.01, 0.003),
    "latency_p95": (250.0, 30.0),
    "cpu_usage": (0.45, 0.05),
    "memory_usage": (0.55, 0.04),
}
ANOMALY = {"error_rate": 0.15, "latency_p95": 1400.0, "cpu_usage": 0.97, "memory_usage": 0.95}
REQUESTS_PER_READING = 500


def post(service: str, values: dict[str, float]) -> dict:
    body = _api.post(
        "/metrics/ingest-batch",
        {"service_name": service, **{k: round(v, 4) for k, v in values.items()}, "requests": REQUESTS_PER_READING},
    )
    flagged = [name for name in ("zscore", "seasonal", "isolation_forest") if body[f"{name}_flagged"]]
    print(
        f"  err={values['error_rate']:.3f} p95={values['latency_p95']:7.1f}ms "
        f"cpu={values['cpu_usage']:.2f} mem={values['memory_usage']:.2f}  "
        f"layers={','.join(flagged) or '-':32s} agree={body['agreeing_layers']}"
        + ("  -> incident" if body["auto_action"] else "")
    )
    return body


def run(service: str, warmup: int, inject: bool, interval: float) -> None:
    print(f"[baseline] {warmup} normal readings for {service}")
    for _ in range(warmup):
        post(service, {m: max(0.0, random.gauss(mu, sd)) for m, (mu, sd) in BASELINE.items()})
        time.sleep(interval)
    if not inject:
        return
    print("[inject] readings far outside the baseline; K=3 in a row per layer, two layers to act")
    for _ in range(5):
        post(service, {m: v * random.uniform(0.95, 1.05) for m, v in ANOMALY.items()})
        time.sleep(interval)
    incidents = [i for i in _api.get("/incidents") if i["service_name"] == service and i["status"] != "RESOLVED"]
    if incidents:
        print(f"[done] incident {incidents[0]['id']}: {incidents[0]['trigger_reason']}")
    else:
        print("[done] no incident: fewer than two layers agreed (is the warmup long enough?)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=_api.BASE)
    ap.add_argument("--service", default="external-api")
    ap.add_argument("--warmup", type=int, default=80)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--inject", action="store_true", help="inject an anomaly burst after the warmup")
    args = ap.parse_args()
    _api.BASE = args.base_url.rstrip("/")
    run(args.service, args.warmup, args.inject, args.interval)
