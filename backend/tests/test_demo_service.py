"""The in-cluster demo workload, run as real processes talking real HTTP.

Two instances: ``ledger`` and ``payments`` (which depends on ledger). Faults
injected into ledger must surface in payments' responses, as they do between
pods in the cluster.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.services.k8s_client import parse_prometheus_text

SCRIPT = Path(__file__).resolve().parents[1] / "app" / "k8s" / "demo_service.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read().decode(), exc.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def _start(env: dict) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = int(env["PORT"])
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("demo service did not start")


@pytest.fixture()
def pair():
    ledger_port, payments_port = _free_port(), _free_port()
    common = {"BASE_LATENCY_MS": "2", "LATENCY_JITTER_MS": "1", "CPU_WORK_ITERS": "10", "DEP_TIMEOUT_S": "1"}
    ledger = _start({**common, "SERVICE_NAME": "ledger-svc", "PORT": str(ledger_port), "STARTUP_DELAY_S": "0"})
    payments = _start(
        {
            **common,
            "SERVICE_NAME": "payments-svc",
            "PORT": str(payments_port),
            "DEPENDENCIES": "127.0.0.1",
            "DEP_PORT": str(ledger_port),
            "STARTUP_DELAY_S": "1",
        }
    )
    yield f"http://127.0.0.1:{ledger_port}", f"http://127.0.0.1:{payments_port}"
    for p in (ledger, payments):
        p.kill()
        p.wait()


def test_work_calls_dependency(pair):
    _, payments = pair
    code, body = _get(f"{payments}/work")
    assert code == 200
    assert body["calls"][0]["ok"] is True


def test_readiness_waits_for_startup_delay():
    # Its own process, with a delay wide enough that a loaded machine cannot
    # make the first probe land after the warm-up has already finished.
    delay_s = 3.0
    port = _free_port()
    proc = _start({"SERVICE_NAME": "warmup-svc", "PORT": str(port), "STARTUP_DELAY_S": str(delay_s)})
    try:
        started = time.monotonic()
        assert _get(f"http://127.0.0.1:{port}/readyz")[0] == 503
        assert _get(f"http://127.0.0.1:{port}/healthz")[0] == 200  # live before it is ready

        while _get(f"http://127.0.0.1:{port}/readyz")[0] != 200:
            assert time.monotonic() - started < delay_s + 5, "never became ready"
            time.sleep(0.1)
        assert time.monotonic() - started >= delay_s - 0.5
    finally:
        proc.kill()
        proc.wait()


def test_error_fault_cascades_with_root_cause(pair):
    ledger, payments = pair
    code, _ = _get(f"{ledger}/admin/fault", "POST", {"type": "error", "magnitude": 1.0, "duration_s": 30})
    assert code == 200
    code, body = _get(f"{payments}/work")
    assert code == 502
    assert body["error_signature"] == "UpstreamError:payments-svc->127.0.0.1:500"
    assert body["caused_by"] == "InternalError:ledger-svc@v0.0.0"  # VERSION unset in this test

    _get(f"{ledger}/admin/fault", "DELETE")
    assert _get(f"{payments}/work")[0] == 200


def test_latency_fault_becomes_upstream_timeout(pair):
    ledger, payments = pair
    _get(f"{ledger}/admin/fault", "POST", {"type": "latency", "magnitude": 1500, "duration_s": 30})
    code, body = _get(f"{payments}/work")
    assert code == 502
    assert body["error_signature"].startswith("UpstreamTimeout:payments-svc")


def test_fault_expires_on_its_own(pair):
    ledger, _ = pair
    _get(f"{ledger}/admin/fault", "POST", {"type": "error", "magnitude": 1.0, "duration_s": 0.3})
    assert _get(f"{ledger}/work")[0] == 500
    time.sleep(0.4)
    assert _get(f"{ledger}/work")[0] == 200


def test_metrics_expose_process_cpu_and_memory(pair):
    ledger, _ = pair
    _, text = _get(f"{ledger}/metrics")
    parsed = parse_prometheus_text(text)
    assert parsed["process_cpu_seconds_total"] >= 0
    assert parsed["process_resident_memory_bytes"] > 1_000_000
