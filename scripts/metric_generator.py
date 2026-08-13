#!/usr/bin/env python3
"""Simulated metric generator.

Posts a realistic metric stream to the backend's /metrics/ingest endpoint:
first a stable baseline (so the z-score detector can learn each metric's
normal), then an injected anomaly burst that should trip the detector and
auto-open an incident.

Usage:
    python scripts/metric_generator.py --service payments --base-url http://localhost:8000
    python scripts/metric_generator.py --service payments --inject   # force anomaly burst
"""
from __future__ import annotations

import argparse
import random
import time

import httpx

BASELINES = {
    "error_rate": (0.01, 0.003),      # (mean, stddev)
    "latency_p95": (250.0, 30.0),
    "cpu_usage": (0.45, 0.08),
    "memory_usage": (0.55, 0.06),
}

ANOMALY = {
    "error_rate": 0.15,
    "latency_p95": 1400.0,
    "cpu_usage": 0.97,
    "memory_usage": 0.95,
}


def post(client: httpx.Client, base_url: str, service: str, metric: str, value: float) -> None:
    r = client.post(
        f"{base_url}/metrics/ingest",
        json={"service_name": service, "metric_name": metric, "value": round(value, 4)},
        timeout=10,
    )
    z = r.json().get("z_score")
    z_str = f"z={z:.2f}" if z is not None else "z=warmup"
    print(f"  {metric:14s} = {value:8.3f}  {z_str}")


def run(base_url: str, service: str, warmup: int, inject: bool, interval: float) -> None:
    with httpx.Client() as client:
        print(f"[baseline] warming up {warmup} readings for {service} ...")
        for i in range(warmup):
            for metric, (mu, sigma) in BASELINES.items():
                post(client, base_url, service, metric, max(0.0, random.gauss(mu, sigma)))
            time.sleep(interval)

        if inject:
            print("[inject] pushing anomaly burst (should trip detector) ...")
            for _ in range(4):  # > K consecutive
                for metric, value in ANOMALY.items():
                    jitter = value * random.uniform(0.95, 1.05)
                    post(client, base_url, service, metric, jitter)
                time.sleep(interval)
            print("[done] check /incidents for an auto-created incident.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--service", default="payments")
    ap.add_argument("--warmup", type=int, default=15)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--inject", action="store_true", help="inject an anomaly burst after warmup")
    args = ap.parse_args()
    run(args.base_url, args.service, args.warmup, args.inject, args.interval)
