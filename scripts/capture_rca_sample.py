#!/usr/bin/env python3
"""One-command proof that the RCA engine actually talks to a real LLM.

Runs a failing deployment, waits for the auto-created incident, triggers the
RCA pipeline, and saves the ranked output to docs/rca_sample.json as a committed
sample that the RCA runs against a real model, not just the rule-based fallback.

Prereq: the backend must be running WITH a real key so the LLM path (not the
fallback) executes:

    cd backend
    export ANTHROPIC_API_KEY=sk-ant-...
    HEALTH_CHECK_FAST=1 uvicorn app.main:app

Then, from the repo root:

    python scripts/capture_rca_sample.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
OUT = Path(__file__).resolve().parent.parent / "docs" / "rca_sample.json"


def wait_terminal(client: httpx.Client, dep_id: str, tries: int = 40) -> str:
    for _ in range(tries):
        status = client.get(f"{BASE}/deployments/{dep_id}").json()["status"]
        if status in ("LIVE", "ROLLED_BACK"):
            return status
        time.sleep(0.5)
    return status


def main() -> int:
    with httpx.Client(timeout=60) as client:
        print("→ triggering a failing deployment ...")
        dep = client.post(
            f"{BASE}/deployments",
            json={"service_name": "checkout", "version": "v9.9.9", "simulate_failure": True},
        ).json()
        status = wait_terminal(client, dep["id"])
        print(f"  deployment status: {status}")
        if status != "ROLLED_BACK":
            print("! expected ROLLED_BACK — is the backend running?")
            return 1

        incidents = client.get(f"{BASE}/incidents").json()
        incident_id = incidents[0]["id"]
        print(f"→ incident {incident_id} — running RCA ...")

        report = client.post(f"{BASE}/incidents/{incident_id}/trigger-rca").json()

    model = report.get("llm_model_used", "")
    OUT.write_text(json.dumps(report, indent=2))
    print(f"→ saved {OUT}")

    if model == "rule-based-fallback":
        print(
            "\n⚠  RCA used the RULE-BASED FALLBACK, not a real LLM.\n"
            "   Set ANTHROPIC_API_KEY on the backend and re-run to capture the\n"
            "   real LLM output. (The fallback proves graceful degradation; this\n"
            "   sample is meant to capture the real model path.)"
        )
        return 2

    print(f"\n✓ RCA ran against real model: {model}")
    for i, c in enumerate(report["root_cause_candidates"], 1):
        print(f"  #{i} [{c['confidence']}] {c['cause']}\n      evidence: {c['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
