#!/usr/bin/env python3
"""Smoke test against a real cluster: the checks CI runs on a fresh kind cluster.

Needs a backend in kind mode (``CLUSTER_MODE=kind``). Each step asserts on what
the cluster actually did, not only on what the API reports:

1. bootstrap: every Service and Deployment exists, the live colours are Ready,
   and synthetic traffic flows
2. an unpullable image fails its rollout fast and never takes traffic
3. a blue-green rollout switches the live Service's selector
4. an error burst on inventory-svc is detected by the system's own signals,
   and the services that call it are filed as cascading from it

Usage:
    python scripts/kind_smoke.py        # BASE_URL defaults to http://localhost:8000
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from _api import get, post


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def wait_for(what: str, check: Callable[[], Any], timeout_s: float, every_s: float = 3) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            sys.exit(f"FAIL: timed out after {timeout_s:.0f}s waiting for {what}")
        time.sleep(every_s)


def deployment_done(dep_id: str) -> Callable[[], dict | None]:
    def check() -> dict | None:
        d = get(f"/deployments/{dep_id}")
        return d if d["status"] in ("LIVE", "ROLLED_BACK") else None

    return check


def expect(cond: bool, msg: str) -> None:
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"  ok  {msg}", flush=True)


def main() -> int:
    step("bootstrap")
    cluster = wait_for(
        "the backend to bootstrap and traffic to flow",
        lambda: (c := get("/cluster"))["bootstrapped"] and c["load_generator"]["serving"] and c,
        300,
    )
    expect(cluster["mode"] == "kind", f"backend drives a real cluster ({cluster['server_version']})")
    expect(all(n["ready"] for n in cluster["nodes"]), f"{len(cluster['nodes'])} node(s) Ready")
    snap = wait_for(
        "measured traffic on every service",
        lambda: (
            (s := get("/console/snapshot"))
            and all(x["traffic"] and x["traffic"]["requests"] for x in s["services"])
            and s
        ),
        60,
    )
    expect(len(snap["services"]) == 6, "six services serving measured traffic")

    step("an unpullable image fails its rollout fast")
    bad = post(
        "/deployments",
        {"service_name": "catalog-svc", "version": "v0.0.1-bad", "image": "ghcr.io/opsforge/does-not-exist:v0"},
    )
    d = wait_for("the bad rollout to finish", deployment_done(bad["id"]), 180)
    reason = next(h["reason"] for h in d["stage_history"] if h["stage"] == "ROLLING_BACK")
    expect(d["status"] == "ROLLED_BACK", f"rolled back: {reason}")
    expect("ImagePull" in reason or "ErrImage" in reason, "the pod's image-pull failure was the reason")
    expect(not any(h["stage"].startswith("CANARY") for h in d["stage_history"]), "the candidate never took traffic")

    step("a blue-green rollout switches the live selector")
    dep = post("/deployments", {"service_name": "ledger-svc", "version": "v1.1.0", "strategy": "BLUE_GREEN"})
    d = wait_for("the blue-green rollout to finish", deployment_done(dep["id"]), 300)
    expect(d["status"] == "LIVE", f"LIVE: {d['status_reason']}")
    k8s = get(f"/deployments/{dep['id']}/k8s-status")
    expect(k8s["live_color"] == d["target_color"], f"live Service selector now on {k8s['live_color']}")
    expect(k8s["candidate"]["version"] == "v1.1.0", "the new colour's pods run v1.1.0")

    step("an error burst is detected and its cascade classified")
    drill_id = post(
        "/chaos/inject",
        {"service_name": "inventory-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0, "duration_s": 30},
    )["id"]
    exp = wait_for(
        "the drill to complete",
        lambda: (e := get(f"/chaos/experiments/{drill_id}"))["status"] == "COMPLETED" and e,
        360,
        5,
    )
    expect(exp["passed"] is True, exp["notes"])
    incidents = [i for i in get("/incidents") if i["chaos_experiment_id"] == exp["id"]]
    root = next((i for i in incidents if i["service_name"] == "inventory-svc"), None)
    expect(root is not None and not root["is_cascading"], "inventory-svc holds the root incident")
    children = [i["service_name"] for i in get(f"/incidents/{root['id']}/cascade-tree")["children"]]
    expect("checkout-api" in children, f"its callers cascade from it: {', '.join(children)}")

    print("\nsmoke test passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
