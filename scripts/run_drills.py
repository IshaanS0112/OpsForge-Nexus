#!/usr/bin/env python3
"""Run the chaos drill suite against a running backend and record the results.

One drill per fault type, plus a canary-linked drill:

    latency   LATENCY_INJECTION  ledger-svc     +400 ms for 60 s
    errors    ERROR_BURST        inventory-svc  half of all requests fail for 60 s
    pod-kill  POD_KILL           payments-svc   every pod deleted; the ReplicaSet replaces it
    canary    ERROR_BURST        catalog-svc    the canary colour of a rollout fails half its
                                                requests; its gate should roll it back

Before each drill the script waits until every service is quiet (no open
incident, no FAST_BURN page, no rollout or drill in flight) and until the
z-score and Isolation Forest layers are warm, so drills do not bleed into each
other and consensus can vote. After each drill it ranks the root incident's
causes twice, told about the drill and blind, and records whether the blind
ranking named the drilled service.

Each drill is written to docs/drills/<timestamp>-<name>.json, and the table in
docs/drills/README.md is regenerated from every record there. Every record
carries the cluster mode, so a simulated run is never mistaken for a real one.

Usage:
    python3 scripts/run_drills.py                    # the whole suite
    python3 scripts/run_drills.py --only pod-kill    # one drill
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from _api import ApiError, get, post

OUT = Path(__file__).resolve().parent.parent / "docs" / "drills"

SUITE = {
    "latency": {"service_name": "ledger-svc", "fault_type": "LATENCY_INJECTION", "magnitude": 400},
    "errors": {"service_name": "inventory-svc", "fault_type": "ERROR_BURST", "magnitude": 0.5},
    "pod-kill": {"service_name": "payments-svc", "fault_type": "POD_KILL", "magnitude": 1.0},
    "canary": {"service_name": "catalog-svc", "fault_type": "ERROR_BURST", "magnitude": 0.5, "target": "canary"},
}
DURATION_S = 60


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def quiet_problems(snap: dict, require_warm: bool) -> list[str]:
    """Why the system is not ready for the next drill (empty when it is)."""
    problems = []
    for s in snap["services"]:
        if s["incident"]:
            problems.append(f"{s['name']} has an open incident")
        if s["deployment"]:
            problems.append(f"{s['name']} has a rollout in flight")
        # Only a page matters here. The slower tickets (MEDIUM_BURN over 30 min,
        # SLOW_BURN over 6 h) keep firing long after a one-minute drill, and
        # they play no part in detecting or rolling back the next one.
        if (s["burn"] or {}).get("worst") == "FAST_BURN":
            problems.append(f"{s['name']} FAST_BURN")
        consensus = s["consensus"] or {}
        if consensus.get("agreeing_layers"):
            problems.append(f"{s['name']} {consensus['agreeing_layers']} layer(s) flagging")
        layers = consensus.get("layers") or {}
        if require_warm and not all((layers.get(k) or {}).get("warm") for k in ("zscore", "isolation_forest")):
            problems.append(f"{s['name']} detectors warming up")
    if snap["drills"]:
        problems.append("a drill is running")
    return problems


def wait_quiet(require_warm: bool, timeout_s: float = 1200) -> None:
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        problems = quiet_problems(get("/console/snapshot"), require_warm)
        if not problems:
            return
        summary = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
        if summary != last:
            log(f"waiting for quiet: {summary}")
            last = summary
        if time.monotonic() > deadline:
            sys.exit(f"system did not go quiet within {timeout_s:.0f}s: {summary}")
        time.sleep(5)


def start_canary(service: str) -> str:
    version = f"v1.9.{int(time.time())}"
    dep_id = post("/deployments", {"service_name": service, "version": version})["id"]
    log(f"canary {version} of {service} started ({dep_id})")
    for _ in range(120):
        status = get(f"/deployments/{dep_id}")["status"]
        if status.startswith("CANARY_"):
            log(f"canary taking traffic at {status}")
            return dep_id
        if status in ("LIVE", "ROLLED_BACK"):
            sys.exit(f"canary ended at {status} before the drill could target it")
        time.sleep(2)
    sys.exit("canary never reached a canary stage")


def ranking(report: dict) -> list[dict]:
    return [
        {"cause": c["cause"], "confidence": c["confidence"], "evidence": c["evidence"]}
        for c in report["root_cause_candidates"]
    ]


def run_drill(name: str, cluster: dict) -> dict:
    spec = {**SUITE[name], "duration_s": DURATION_S}
    deployment_id = start_canary(spec["service_name"]) if spec.get("target") == "canary" else None

    try:
        exp_id = post("/chaos/inject", spec)["id"]
    except ApiError as exc:
        sys.exit(f"inject failed: {exc}")
    log(f"{name}: {spec['fault_type']} on {spec['service_name']} injected ({exp_id})")
    while (exp := get(f"/chaos/experiments/{exp_id}"))["status"] != "COMPLETED":
        time.sleep(3)
    log(f"{name}: {exp['notes']}")

    incidents = [i for i in get("/incidents") if i["chaos_experiment_id"] == exp_id]
    root = next((i for i in incidents if i["service_name"] == spec["service_name"]), None)
    record: dict = {
        "drill": name,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cluster": {k: cluster.get(k) for k in ("mode", "context", "server_version", "namespace")},
        "spec": spec,
        "experiment": exp,
        "deployment": get(f"/deployments/{deployment_id}") if deployment_id else None,
        "incidents": incidents,
        "root_incident_id": root["id"] if root else None,
        "cascade": [],
        "rca": None,
    }
    if root is None:
        log(f"{name}: no incident on {spec['service_name']} was linked to the drill")
        return record

    tree = get(f"/incidents/{root['id']}/cascade-tree")
    record["cascade"] = [c["service_name"] for c in tree["children"]]
    told = post(f"/incidents/{root['id']}/trigger-rca")
    blind = post(f"/incidents/{root['id']}/trigger-rca", blind="true")
    top = blind["root_cause_candidates"][0]["cause"] if blind["root_cause_candidates"] else ""
    record["rca"] = {
        "model": told["llm_model_used"],
        "told": ranking(told),
        "blind": ranking(blind),
        "blind_top_names_service": spec["service_name"] in top,
    }
    log(f"{name}: cascade {record['cascade'] or 'none'}; blind RCA #1: {top or '-'} ({blind['llm_model_used']})")
    return record


def fmt_s(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f} s"


def mechanisms(exp: dict) -> str:
    notes = exp.get("notes") or ""
    return notes[notes.find("(") + 1 : notes.rfind(")")] if "(" in notes else "—"


def write_summary() -> None:
    rows = []
    for path in sorted(OUT.glob("*.json")):
        r = json.loads(path.read_text())
        exp, rca = r["experiment"], r.get("rca") or {}
        spec = r["spec"]
        mag = spec["magnitude"]
        magnitude = f"+{mag:g} ms" if spec["fault_type"] == "LATENCY_INJECTION" else f"{mag:.0%}"
        blind = (rca.get("blind") or [{}])[0].get("cause", "—")
        rows.append(
            f"| [{r['recorded_at'][:16].replace('T', ' ')}]({path.name}) | {r['cluster']['mode']} "
            f"| {spec['fault_type']} {magnitude} | {spec['service_name']} ({spec.get('target', 'all')}) "
            f"| {'PASS' if exp['passed'] else 'FAIL'} | {fmt_s(exp['detection_latency_seconds'])} "
            f"| {mechanisms(exp)} | {fmt_s(exp['rollback_latency_seconds'])} "
            f"| {fmt_s(exp['recovery_latency_seconds'])} | {', '.join(r['cascade']) or '—'} | {blind} |"
        )
    (OUT / "README.md").write_text(
        "# Chaos drill records\n\n"
        "Written by [`scripts/run_drills.py`](../../scripts/run_drills.py); one JSON file per drill,\n"
        "with the experiment, the incidents it opened, the cascade tree and both RCA rankings.\n"
        "Latencies are measured from injection on the control loop's own signals. A drill\n"
        "passes when it is detected within 90 s and, if it targets a canary, rolled back\n"
        "within 90 s. The last column is RCA's top cause with the drill withheld.\n\n"
        "| Recorded (UTC) | Cluster | Fault | Service (target) | Verdict | Detected | Mechanisms "
        "| Rolled back | Recovered | Cascaded to | Blind RCA #1 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(SUITE), action="append", help="run only these drills")
    ap.add_argument("--no-warm", action="store_true", help="do not wait for the detector layers to warm up")
    args = ap.parse_args()
    names = args.only or list(SUITE)

    OUT.mkdir(parents=True, exist_ok=True)
    cluster = get("/cluster")
    log(f"{cluster['mode']} cluster ({cluster.get('context')}); drills: {', '.join(names)}")
    for name in names:
        wait_quiet(require_warm=not args.no_warm)
        record = run_drill(name, cluster)
        stamp = record["recorded_at"][:19].replace(":", "").replace("-", "")
        path = OUT / f"{stamp}-{name}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        write_summary()
        log(f"{name}: recorded {path.relative_to(OUT.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
