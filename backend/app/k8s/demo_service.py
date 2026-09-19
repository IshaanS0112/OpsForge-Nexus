"""OpsForge demo workload: one small HTTP service per catalog entry.

Runs as real pods in the kind cluster on the stock ``python:3.12-alpine``
image; the backend ships this file in a ConfigMap, so there is no image to
build or side-load. Standard library only.

What makes it useful as a test subject:

* ``GET /work`` does a little local work behind a bounded worker pool (so
  latency genuinely rises with load), then calls every dependency's
  ``/work`` over cluster DNS. A slow or failing dependency therefore makes
  this service slow or failing too: cascades propagate through real calls.
* ``/admin/fault`` accepts runtime faults (added latency, error burst) with a
  TTL enforced here, so a fault clears itself even if the controller dies.
* Release-level misbehaviour comes from environment (``FAULT_ERROR_RATE``,
  ``FAULT_LATENCY_MS``), which a deployment's ``config_diff`` sets: a bad
  config ships with the version, like a real bad release.
* ``/metrics`` exposes Prometheus-format process CPU and resident memory.
* ``/readyz`` fails for ``STARTUP_DELAY_S`` after boot to model warm-up, so a
  killed pod leaves a real gap before its replacement takes traffic.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import resource
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE_NAME", "demo-svc")
VERSION = os.environ.get("APP_VERSION", "v0.0.0")
COLOR = os.environ.get("COLOR", "blue")
DEPENDENCIES = [d.strip() for d in os.environ.get("DEPENDENCIES", "").split(",") if d.strip()]
PORT = int(os.environ.get("PORT", "8080"))
DEP_PORT = int(os.environ.get("DEP_PORT", "8080"))
BASE_LATENCY_MS = float(os.environ.get("BASE_LATENCY_MS", "20"))
JITTER_MS = float(os.environ.get("LATENCY_JITTER_MS", "5"))
WORKERS = int(os.environ.get("WORKERS", "4"))
QUEUE_TIMEOUT_S = float(os.environ.get("QUEUE_TIMEOUT_S", "1.0"))
CPU_WORK_ITERS = int(os.environ.get("CPU_WORK_ITERS", "300"))
STARTUP_DELAY_S = float(os.environ.get("STARTUP_DELAY_S", "0"))
DEP_TIMEOUT_S = float(os.environ.get("DEP_TIMEOUT_S", "2.0"))
RELEASE_ERROR_RATE = float(os.environ.get("FAULT_ERROR_RATE", "0"))
RELEASE_LATENCY_MS = float(os.environ.get("FAULT_LATENCY_MS", "0"))
POD = socket.gethostname()
STARTED = time.monotonic()

FAULT_KINDS = ("latency", "error")


class Faults:
    """Runtime faults with expiry. ``latency`` magnitude is milliseconds of
    added delay; ``error`` magnitude is the probability of a 500."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: dict[str, tuple[float, float]] = {}

    def set(self, kind: str, magnitude: float, duration_s: float) -> None:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}")
        with self._lock:
            self._faults[kind] = (float(magnitude), time.monotonic() + float(duration_s))

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def active(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            for kind in [k for k, (_, exp) in self._faults.items() if exp <= now]:
                del self._faults[kind]
            return {k: mag for k, (mag, _) in self._faults.items()}

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {
                k: {"magnitude": mag, "expires_in_s": round(exp - now, 1)}
                for k, (mag, exp) in self._faults.items()
                if exp > now
            }


FAULTS = Faults()
WORK_SLOTS = threading.BoundedSemaphore(WORKERS)
_stats_lock = threading.Lock()
_requests_by_code: dict[int, int] = {}
_inflight = 0


def _count(code: int) -> None:
    with _stats_lock:
        _requests_by_code[code] = _requests_by_code.get(code, 0) + 1


def burn_cpu(iterations: int) -> None:
    digest = POD.encode()
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()


def resident_memory_bytes() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    # Outside Linux (local runs): peak RSS is the best the stdlib offers.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if peak > 1 << 24 else peak * 1024


def render_metrics() -> str:
    with _stats_lock:
        by_code = dict(_requests_by_code)
        inflight = _inflight
    lines = [
        "# TYPE process_cpu_seconds_total counter",
        f"process_cpu_seconds_total {time.process_time():.6f}",
        "# TYPE process_resident_memory_bytes gauge",
        f"process_resident_memory_bytes {resident_memory_bytes()}",
        "# TYPE opsforge_inflight_requests gauge",
        f"opsforge_inflight_requests {inflight}",
        "# TYPE opsforge_requests_total counter",
    ]
    lines += [f'opsforge_requests_total{{code="{code}"}} {n}' for code, n in sorted(by_code.items())]
    lines.append(f'opsforge_build_info{{service="{SERVICE}",version="{VERSION}",color="{COLOR}"}} 1')
    return "\n".join(lines) + "\n"


def call_dependency(dep: str) -> dict:
    """Call a dependency's /work over cluster DNS. Returns a call record; a
    failed call carries the signature this service will report upstream."""
    start = time.monotonic()
    url = f"http://{dep}:{DEP_PORT}/work"
    record: dict = {"dependency": dep}
    try:
        with urllib.request.urlopen(url, timeout=DEP_TIMEOUT_S) as resp:
            resp.read()
            record.update(ok=True, status=resp.status)
    except urllib.error.HTTPError as exc:
        caused_by = None
        try:
            body = json.loads(exc.read() or b"{}")
            caused_by = body.get("caused_by") or body.get("error_signature")
        except (ValueError, OSError):
            pass
        record.update(
            ok=False,
            status=exc.code,
            signature=f"UpstreamError:{SERVICE}->{dep}:{exc.code}",
            caused_by=caused_by,
        )
    except TimeoutError:
        record.update(ok=False, status=None, signature=f"UpstreamTimeout:{SERVICE}->{dep}")
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
        kind = "UpstreamTimeout" if timed_out else "UpstreamUnavailable"
        record.update(ok=False, status=None, signature=f"{kind}:{SERVICE}->{dep}")
    except OSError:
        record.update(ok=False, status=None, signature=f"UpstreamUnavailable:{SERVICE}->{dep}")
    record["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    return record


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"opsforge-demo/{VERSION}"

    def log_message(self, *_args) -> None:  # request logs would drown the pod log
        pass

    # ---- plumbing ----
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Served-By-Color", COLOR)
        self.send_header("X-Served-By-Pod", POD)
        self.send_header("X-Service-Version", VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    # ---- routes ----
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/work":
            self._work()
        elif path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/readyz":
            warm = time.monotonic() - STARTED >= STARTUP_DELAY_S
            self._json(200 if warm else 503, {"ready": warm})
        elif path == "/admin/fault":
            self._json(200, {"pod": POD, "faults": FAULTS.snapshot()})
        elif path == "/metrics":
            self._send(200, render_metrics().encode(), "text/plain; version=0.0.4")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/admin/fault":
            self._json(404, {"error": "not found"})
            return
        try:
            body = self._read_json()
            FAULTS.set(body["type"], body["magnitude"], body.get("duration_s", 60))
        except (KeyError, ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"pod": POD, "faults": FAULTS.snapshot()})

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] != "/admin/fault":
            self._json(404, {"error": "not found"})
            return
        FAULTS.clear()
        self._json(200, {"pod": POD, "faults": {}})

    def _work(self) -> None:
        global _inflight
        start = time.monotonic()
        with _stats_lock:
            _inflight += 1
        try:
            code, payload = self._do_work()
        finally:
            with _stats_lock:
                _inflight -= 1
        payload.update(
            service=SERVICE,
            version=VERSION,
            color=COLOR,
            pod=POD,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
        _count(code)
        self._json(code, payload)

    def _do_work(self) -> tuple[int, dict]:
        faults = FAULTS.active()
        if not WORK_SLOTS.acquire(timeout=QUEUE_TIMEOUT_S):
            return 503, {"error_signature": f"Overloaded:{SERVICE}"}
        try:
            time.sleep(max(0.0, BASE_LATENCY_MS + random.uniform(-JITTER_MS, JITTER_MS)) / 1000)
            burn_cpu(CPU_WORK_ITERS)
        finally:
            WORK_SLOTS.release()

        # Added latency models waiting on something external, so it does not
        # hold a worker slot.
        extra_ms = RELEASE_LATENCY_MS + faults.get("latency", 0.0)
        if extra_ms > 0:
            time.sleep(extra_ms / 1000)
        # A bad release and an injected burst fail the same way and log the
        # same signature: the handler cannot tell why it failed, and neither
        # can anything reading its logs. Telling them apart is RCA's job.
        release_failed = RELEASE_ERROR_RATE > 0 and random.random() < RELEASE_ERROR_RATE
        if release_failed or random.random() < faults.get("error", 0.0):
            return 500, {"error_signature": f"InternalError:{SERVICE}@{VERSION}"}

        calls = []
        for dep in DEPENDENCIES:
            record = call_dependency(dep)
            calls.append(record)
            if not record["ok"]:
                return 502, {
                    "error_signature": record["signature"],
                    "caused_by": record.get("caused_by") or record["signature"],
                    "calls": calls,
                }
        return 200, {"calls": calls}


def serve(port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    srv = serve()
    print(f"{SERVICE} {VERSION} ({COLOR}) on :{PORT} deps={DEPENDENCIES}", flush=True)
    srv.serve_forever()
