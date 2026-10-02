#!/usr/bin/env python3
"""One-command check that the RCA engine talks to a real model.

Ships a bad-config canary, waits for its gate to roll it back, triggers RCA on
the incident the rollback opened, and writes the ranked output to
docs/rca_sample.json. The sample is evidence that RCA ran against the model
rather than the rule-based fallback; it is git-ignored, since it would embed
one account's model output in the repository.

Prereq: the backend must run WITH a key so the model path (not the fallback)
executes, e.g. ``ANTHROPIC_API_KEY=sk-ant-... docker compose up -d``. Then,
from the repo root:

    python3 scripts/capture_rca_sample.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from _api import ApiError, get, post

SERVICE = os.environ.get("SERVICE", "payments-svc")
OUT = Path(__file__).resolve().parent.parent / "docs" / "rca_sample.json"


def wait_terminal(dep_id: str, timeout_s: float = 600) -> str:
    deadline = time.monotonic() + timeout_s
    status = "?"
    while time.monotonic() < deadline:
        dep = get(f"/deployments/{dep_id}")
        if dep["status"] != status:
            status = dep["status"]
            print(f"  {status}  {dep.get('status_reason') or ''}")
        if status in ("LIVE", "ROLLED_BACK"):
            break
        time.sleep(2)
    return status


def main() -> int:
    print(f"-> shipping a bad-config canary on {SERVICE} ...")
    try:
        dep_id = post(
            "/deployments", {"service_name": SERVICE, "version": f"v9.{int(time.time())}", "simulate_failure": True}
        )["id"]
    except ApiError as exc:
        print(f"! could not start the rollout: {exc}")
        return 1
    status = wait_terminal(dep_id)
    if status != "ROLLED_BACK":
        print(f"! expected ROLLED_BACK, got {status}")
        return 1

    incident = next((i for i in get("/incidents") if i["related_deployment_id"] == dep_id), None)
    if incident is None:
        print("! the rollback opened no incident")
        return 1
    print(f"-> incident {incident['id']}: running RCA ...")
    report = post(f"/incidents/{incident['id']}/trigger-rca")

    OUT.write_text(json.dumps(report, indent=2))
    print(f"-> saved {OUT}")

    model = report.get("llm_model_used", "")
    if model == "rule-based-fallback":
        print(
            "\n!  RCA used the RULE-BASED FALLBACK, not a model.\n"
            "   Start the backend with ANTHROPIC_API_KEY set and re-run to capture\n"
            "   the model path. (The fallback shows graceful degradation; this\n"
            "   sample is meant to capture the model's ranking.)"
        )
        return 2

    print(f"\nRCA ran against {model}")
    for n, c in enumerate(report["root_cause_candidates"], 1):
        print(f"  #{n} [{c['confidence']}] {c['cause']}\n      evidence: {c['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
