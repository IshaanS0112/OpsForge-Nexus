"""Edge gateway: weighted canary routing, synthetic load, and measurement.

Every edge request to a service goes through ``Gateway.send``:

1. ``RoutingTable.choose`` picks what to address. Normally the live Service
   (whose selector decides the colour). During a canary, a weighted random
   choice between the stable colour's Service and the canary colour's
   Service — the traffic split happens here, at the application gateway, not
   in a service mesh.
2. A transport performs the request: real HTTP to the Service's NodePort on
   the kind node, or the in-memory data plane in simulated mode.
3. ``TrafficRecorder`` keeps the outcome (status, latency, which colour
   served it, error signature) until the control loop drains it into
   per-tick metrics, SLI counts and error-signature logs.

Honest limit: only edge (north-south) traffic is split. Pods call their
dependencies through the live Service, so east-west traffic follows the
selector, not the canary weight. Splitting that too needs a mesh.

``LoadGenerator`` produces the synthetic traffic: Poisson arrivals at a rate
that follows a sinusoidal "day" with a scheduled burst at the same point of
every day (a batch job). It is open-loop — the next request is scheduled
independently of the previous response — so slow responses cannot throttle
the offered load and hide latency (coordinated omission).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app import catalog
from app.catalog import ServiceSpec
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    service: str
    variant: str  # what was addressed: "live" | "blue" | "green"
    color: Optional[str]  # colour that served it (from the response), if known
    status: Optional[int]  # None: timeout / connection failure
    latency_ms: float
    signature: Optional[str] = None
    caused_by: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.status is None or self.status >= 500


# ------------------------------------------------------------------ routing --
@dataclass
class Route:
    live_color: str = "blue"
    canary_color: Optional[str] = None
    canary_weight: int = 0  # percent of edge traffic sent to the canary colour


class RoutingTable:
    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._routes: dict[str, Route] = {}
        self._lock = threading.Lock()
        self._rng = rng or random.Random()

    def set_live(self, service: str, color: str) -> None:
        with self._lock:
            self._routes[service] = Route(live_color=color)

    def set_canary(self, service: str, stable_color: str, canary_color: str, weight: int) -> None:
        with self._lock:
            self._routes[service] = Route(stable_color, canary_color, max(0, min(100, weight)))

    def route(self, service: str) -> Route:
        with self._lock:
            return self._routes.get(service) or Route()

    def choose(self, service: str) -> tuple[str, Optional[str]]:
        """-> (variant to address, colour expected to serve it)."""
        r = self.route(service)
        if not r.canary_color or r.canary_weight <= 0:
            return "live", r.live_color
        if r.canary_weight >= 100 or self._rng.random() * 100 < r.canary_weight:
            return r.canary_color, r.canary_color
        return r.live_color, r.live_color

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                s: {"live_color": r.live_color, "canary_color": r.canary_color, "canary_weight": r.canary_weight}
                for s, r in self._routes.items()
            }


# ---------------------------------------------------------------- recording --
@dataclass
class ColorWindow:
    total: int = 0
    bad: int = 0  # 5xx, transport error, or slower than the SLO latency bound
    errors: int = 0  # 5xx or transport error only
    latencies: list[float] = field(default_factory=list)
    # (signature, root signature or None) -> count
    signatures: Counter = field(default_factory=Counter)


@dataclass
class ServiceWindow:
    service: str
    colors: dict[str, ColorWindow] = field(default_factory=dict)

    def _sum(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.colors.values())

    @property
    def total(self) -> int:
        return self._sum("total")

    @property
    def bad(self) -> int:
        return self._sum("bad")

    @property
    def errors(self) -> int:
        return self._sum("errors")

    @property
    def error_rate(self) -> Optional[float]:
        return self.errors / self.total if self.total else None

    @property
    def latency_p95(self) -> Optional[float]:
        values = sorted(v for c in self.colors.values() for v in c.latencies)
        return percentile(values, 0.95) if values else None

    def signatures(self) -> Counter:
        """(signature, root signature or None) -> count, across colours."""
        out: Counter = Counter()
        for c in self.colors.values():
            out.update(c.signatures)
        return out


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_values:
        raise ValueError("empty")
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


class TrafficRecorder:
    """Accumulates outcomes between control-loop ticks. Thread-safe: the load
    generator records from the event loop, the control loop drains from a
    worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, ServiceWindow] = {}
        self._latency_bounds: dict[str, float] = {}

    def set_latency_bound(self, service: str, bound_ms: float) -> None:
        with self._lock:
            self._latency_bounds[service] = bound_ms

    def record(self, o: Outcome) -> None:
        bound = self._latency_bounds.get(o.service, settings.slo_default_latency_ms)
        color = o.color or "unknown"
        with self._lock:
            w = self._windows.setdefault(o.service, ServiceWindow(o.service))
            c = w.colors.setdefault(color, ColorWindow())
            c.total += 1
            c.latencies.append(o.latency_ms)
            if o.is_error:
                c.errors += 1
                c.bad += 1
                if o.signature:
                    root = o.caused_by if o.caused_by and o.caused_by != o.signature else None
                    c.signatures[(o.signature, root)] += 1
            elif o.latency_ms > bound:
                c.bad += 1

    def drain(self) -> dict[str, ServiceWindow]:
        with self._lock:
            out, self._windows = self._windows, {}
        return out


# --------------------------------------------------------------- transports --
def _parse_failure(body: bytes) -> tuple[Optional[str], Optional[str]]:
    import json

    try:
        data = json.loads(body or b"{}")
        return data.get("error_signature"), data.get("caused_by")
    except ValueError:
        return None, None


class HttpTransport:
    """Real HTTP to the Services' NodePorts on the kind node.

    Keep-alive connections are recycled after one second: kube-proxy only
    applies a Service selector change to *new* connections, so long-lived
    connections would keep reaching the old colour after a switch."""

    def __init__(self, cluster, timeout_s: float = settings.gateway_timeout_s) -> None:
        self.cluster = cluster
        self.timeout_s = timeout_s
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=1.0)
        self._async: Optional[httpx.AsyncClient] = None
        self._limits = limits
        self._sync = httpx.Client(timeout=timeout_s, limits=limits)

    def _client(self) -> httpx.AsyncClient:
        if self._async is None:
            self._async = httpx.AsyncClient(timeout=self.timeout_s, limits=self._limits)
        return self._async

    def _outcome(self, spec: ServiceSpec, variant: str, color_hint, resp, started: float) -> Outcome:
        latency = (time.perf_counter() - started) * 1000
        color = resp.headers.get("x-served-by-color") or color_hint
        sig = caused = None
        if resp.status_code >= 500:
            sig, caused = _parse_failure(resp.content)
            sig = sig or f"HTTP{resp.status_code}:{spec.name}"
        return Outcome(spec.name, variant, color, resp.status_code, latency, sig, caused)

    def _failure(self, spec: ServiceSpec, variant: str, color_hint, exc: Exception, started: float) -> Outcome:
        latency = (time.perf_counter() - started) * 1000
        kind = "GatewayTimeout" if isinstance(exc, httpx.TimeoutException) else "ConnectionError"
        return Outcome(spec.name, variant, color_hint, None, latency, f"{kind}:{spec.name}")

    async def request(self, spec: ServiceSpec, variant: str, color_hint: Optional[str]) -> Outcome:
        url = f"{self.cluster.endpoint_url(spec, variant)}/work"
        started = time.perf_counter()
        try:
            resp = await self._client().get(url)
            return self._outcome(spec, variant, color_hint, resp, started)
        except httpx.HTTPError as exc:
            return self._failure(spec, variant, color_hint, exc, started)

    def request_sync(self, spec: ServiceSpec, variant: str, color_hint: Optional[str]) -> Outcome:
        url = f"{self.cluster.endpoint_url(spec, variant)}/work"
        started = time.perf_counter()
        try:
            resp = self._sync.get(url)
            return self._outcome(spec, variant, color_hint, resp, started)
        except httpx.HTTPError as exc:
            return self._failure(spec, variant, color_hint, exc, started)

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
        self._sync.close()


class SimTransport:
    """The simulated cluster's in-memory data plane."""

    def __init__(self, cluster) -> None:
        self.cluster = cluster

    def request_sync(self, spec: ServiceSpec, variant: str, color_hint: Optional[str]) -> Outcome:
        r = self.cluster.handle(spec.name, variant)
        return Outcome(
            spec.name,
            variant,
            r.get("color") or color_hint,
            r["status"],
            r["latency_ms"],
            r.get("signature"),
            r.get("caused_by"),
        )

    async def request(self, spec: ServiceSpec, variant: str, color_hint: Optional[str]) -> Outcome:
        return self.request_sync(spec, variant, color_hint)

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------ gateway --
class Gateway:
    def __init__(self, transport, routing: RoutingTable, recorder: TrafficRecorder) -> None:
        self.transport = transport
        self.routing = routing
        self.recorder = recorder

    async def send(self, service: str) -> Outcome:
        spec = catalog.get(service)
        if spec is None:
            raise KeyError(service)
        variant, color = self.routing.choose(service)
        outcome = await self.transport.request(spec, variant, color)
        self.recorder.record(outcome)
        return outcome

    def send_sync(self, service: str) -> Outcome:
        spec = catalog.get(service)
        if spec is None:
            raise KeyError(service)
        variant, color = self.routing.choose(service)
        outcome = self.transport.request_sync(spec, variant, color)
        self.recorder.record(outcome)
        return outcome

    def probe(self, service: str, color: str, n: int) -> list[Outcome]:
        """Smoke requests straight to one colour (health gate). Not recorded:
        they are the deployment engine's measurement, not user traffic."""
        spec = catalog.get(service)
        return [self.transport.request_sync(spec, color, color) for _ in range(n)]


class LoadGenerator:
    """Open-loop Poisson load against every catalog service."""

    def __init__(
        self,
        gateway: Gateway,
        services: list[str],
        rps: float = settings.loadgen_rps_per_service,
        period_s: float = settings.loadgen_cycle_period_s,
        amplitude: float = settings.loadgen_cycle_amplitude,
        max_inflight: int = settings.gateway_max_inflight,
        seed: Optional[int] = None,
        burst_start: float = settings.loadgen_burst_start,
        burst_share: float = settings.loadgen_burst_share,
        burst_multiplier: float = settings.loadgen_burst_multiplier,
    ) -> None:
        self.gateway = gateway
        self.services = services
        self.rps = rps
        self.period_s = period_s
        self.amplitude = amplitude
        self.burst = (burst_start, burst_start + burst_share, burst_multiplier)
        self.max_inflight = max_inflight
        self._rng = random.Random(seed)
        self._inflight = 0
        self._tasks: list[asyncio.Task] = []
        self.running = False

    def rate(self, t: float) -> float:
        """Target requests/second at wall-clock time ``t``: one sinusoidal
        cycle per ``period_s`` (the compressed "day"), multiplied during the
        daily burst, phase-locked to the clock so restarts do not reset it."""
        if self.period_s <= 0:
            return self.rps
        phase = (t % self.period_s) / self.period_s
        rate = self.rps * (1 + self.amplitude * math.sin(2 * math.pi * phase))
        start, end, multiplier = self.burst
        if start <= phase < end:
            rate *= multiplier
        return max(0.1, rate)

    async def _fire(self, service: str) -> None:
        self._inflight += 1
        try:
            await self.gateway.send(service)
        except Exception:  # noqa: BLE001 — one failed request must not stop the generator
            logger.exception("loadgen request to %s failed", service)
        finally:
            self._inflight -= 1

    async def _drive(self, service: str) -> None:
        pending: set[asyncio.Task] = set()
        while self.running:
            await asyncio.sleep(self._rng.expovariate(self.rate(time.time())))
            if self._inflight >= self.max_inflight:
                continue  # shed load rather than queue without bound
            task = asyncio.create_task(self._fire(service))
            pending.add(task)
            task.add_done_callback(pending.discard)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._tasks = [asyncio.create_task(self._drive(s)) for s in self.services]
        logger.info("load generator started: %s rps/service, %d services", self.rps, len(self.services))

    async def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
