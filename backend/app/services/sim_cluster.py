"""In-memory stand-in for the kind cluster.

Implements the ``ClusterClient`` interface without Docker so the whole control
loop — rollouts, canary gates, detectors, chaos drills — runs in the test suite
and on machines without a cluster. It is deliberately labelled everywhere it
surfaces (``mode == "simulated"`` in /cluster and on the dashboard).

The simulation mirrors the real workload's semantics rather than its
mechanics:

* pods become Ready ``startup_delay_s`` after creation; an image other than the
  configured demo image never pulls (``ErrImagePull``), so bad-image rollouts
  fail the same way they do on kind;
* a request picks a Ready pod of the addressed colour, pays load-dependent
  queueing delay, any release/runtime fault, then calls each dependency's live
  colour — so cascades propagate through the dependency graph like the real
  pods' HTTP calls do;
* no Ready pod means the connection is refused, as with a NodePort Service
  that has no endpoints.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from app import catalog
from app.catalog import ServiceSpec
from app.config import settings
from app.k8s import manifests as m
from app.services.k8s_client import PodInfo, RolloutStatus, summarize_rollout


@dataclass
class _SimPod:
    name: str
    color: str
    version: str
    created_at: float
    ready_at: Optional[float]  # None: never becomes ready
    waiting_reason: Optional[str] = None
    faults: dict[str, tuple[float, float]] = field(default_factory=dict)  # kind -> (magnitude, expires)
    cpu_seconds: float = 0.0
    requests: int = 0

    def ready(self, now: float) -> bool:
        return self.ready_at is not None and now >= self.ready_at

    def active_faults(self, now: float) -> dict[str, float]:
        for kind in [k for k, (_, exp) in self.faults.items() if exp <= now]:
            del self.faults[kind]
        return {k: mag for k, (mag, _) in self.faults.items()}


@dataclass
class _SimColor:
    version: str
    replicas: int
    image: str
    env: dict[str, str]
    changed_at: float
    pods: list[_SimPod] = field(default_factory=list)


@dataclass
class _SimService:
    spec: ServiceSpec
    live_color: str
    colors: dict[str, _SimColor]


class SimulatedCluster:
    mode = "simulated"

    def __init__(
        self,
        namespace: str = settings.k8s_namespace,
        clock: Callable[[], float] = time.monotonic,
        seed: int = 7,
        startup_delay_s: Optional[float] = None,
        replicas: Optional[int] = None,
        image: str = settings.demo_service_image,
    ) -> None:
        self.namespace = namespace
        self.clock = clock
        self.rng = random.Random(seed)
        self.startup_delay_s = settings.demo_startup_delay_s if startup_delay_s is None else startup_delay_s
        self.replicas = settings.demo_replicas if replicas is None else replicas
        self.image = image
        self.services: dict[str, _SimService] = {}
        self.offered_rps: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ helpers --
    def _new_pod(self, service: str, color: str, c: _SimColor) -> _SimPod:
        now = self.clock()
        pullable = c.image == self.image
        return _SimPod(
            name=f"{service}-{color}-{next(self._seq):05d}",
            color=color,
            version=c.version,
            created_at=now,
            ready_at=now + float(c.env.get("STARTUP_DELAY_S", self.startup_delay_s)) if pullable else None,
            waiting_reason=None if pullable else "ErrImagePull",
        )

    def _svc(self, service: str) -> _SimService:
        try:
            return self.services[service]
        except KeyError:
            raise KeyError(f"service {service!r} is not deployed") from None

    def _find_pod(self, name: str) -> tuple[_SimService, _SimColor, _SimPod]:
        for svc in self.services.values():
            for c in svc.colors.values():
                for p in c.pods:
                    if p.name == name:
                        return svc, c, p
        raise KeyError(f"pod {name!r} not found")

    # ---------------------------------------------------------------- info --
    def info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "context": "in-memory",
            "server": None,
            "namespace": self.namespace,
            "reachable": True,
            "server_version": "simulated",
            "nodes": [{"name": "simulated-node", "ready": True, "kubelet": "simulated"}],
        }

    # ------------------------------------------------------------ topology --
    def ensure_base(self) -> None:
        return None

    def ensure_service(self, spec: ServiceSpec) -> None:
        with self._lock:
            if spec.name in self.services:
                return
            now = self.clock()
            colors = {}
            for color in m.COLORS:
                c = _SimColor(
                    version=spec.initial_version,
                    replicas=self.replicas if color == "blue" else 0,
                    image=self.image,
                    env={},
                    changed_at=now,
                )
                c.pods = [self._new_pod(spec.name, color, c) for _ in range(c.replicas)]
                colors[color] = c
            self.services[spec.name] = _SimService(spec=spec, live_color="blue", colors=colors)

    # ------------------------------------------------------------- traffic --
    def live_color(self, service: str) -> Optional[str]:
        return self._svc(service).live_color

    def switch_live(self, service: str, color: str) -> None:
        with self._lock:
            self._svc(service).live_color = color

    # ------------------------------------------------------------- rollout --
    def apply_version(
        self,
        spec: ServiceSpec,
        color: str,
        version: str,
        *,
        config_diff: Optional[dict] = None,
        image: Optional[str] = None,
        replicas: Optional[int] = None,
    ) -> str:
        with self._lock:
            c = self._svc(spec.name).colors[color]
            c.version = version
            c.image = image or self.image
            c.env = m.applied_env(config_diff)
            c.replicas = self.replicas if replicas is None else replicas
            c.changed_at = self.clock()
            c.pods = [self._new_pod(spec.name, color, c) for _ in range(c.replicas)]
            return m.deployment_name(spec.name, color)

    def scale(self, service: str, color: str, replicas: int) -> None:
        with self._lock:
            c = self._svc(service).colors[color]
            c.replicas = replicas
            while len(c.pods) > replicas:
                c.pods.pop()
            while len(c.pods) < replicas:
                c.pods.append(self._new_pod(service, color, c))
            c.changed_at = self.clock()

    def color_version(self, service: str, color: str) -> tuple[Optional[str], int]:
        c = self._svc(service).colors[color]
        return c.version, c.replicas

    def rollout_status(self, service: str, color: str) -> RolloutStatus:
        with self._lock:
            now = self.clock()
            c = self._svc(service).colors[color]
            pods = self._pod_infos(service, color)
            ready = sum(1 for p in c.pods if p.ready(now))
            conditions = []
            if ready < c.replicas and now - c.changed_at > settings.rollout_timeout_s:
                conditions.append(
                    {
                        "type": "Progressing",
                        "status": "False",
                        "reason": "ProgressDeadlineExceeded",
                        "message": f'ReplicaSet "{service}-{color}" has timed out progressing.',
                    }
                )
            return summarize_rollout(
                service=service,
                color=color,
                version=c.version,
                generation=1,
                observed_generation=1,
                desired=c.replicas,
                total=len(c.pods),
                updated=len(c.pods),
                ready=ready,
                available=ready,
                conditions=conditions,
                pods=pods,
            )

    # ---------------------------------------------------------------- pods --
    def _pod_infos(self, service: str, color: Optional[str]) -> list[PodInfo]:
        now = self.clock()
        svc = self._svc(service)
        out = []
        for col, c in svc.colors.items():
            if color and col != color:
                continue
            for p in c.pods:
                out.append(
                    PodInfo(
                        name=p.name,
                        color=col,
                        version=p.version,
                        phase="Running" if p.ready_at is not None else "Pending",
                        ready=p.ready(now),
                        waiting_reason=p.waiting_reason,
                        node="simulated-node",
                    )
                )
        return out

    def list_pods(self, service: str, color: Optional[str] = None) -> list[PodInfo]:
        with self._lock:
            return self._pod_infos(service, color)

    def delete_pod(self, name: str) -> None:
        """Like a ReplicaSet: the deleted pod is replaced by a fresh one that
        must warm up again before it is Ready."""
        with self._lock:
            svc, c, pod = self._find_pod(name)
            c.pods.remove(pod)
            c.pods.append(self._new_pod(svc.spec.name, pod.color, c))

    def pod_request(
        self, pod: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0
    ) -> tuple[int, str]:
        import json

        with self._lock:
            try:
                _, _, p = self._find_pod(pod)
            except KeyError:
                return 404, '{"error": "pod not found"}'
            now = self.clock()
            path = path.lstrip("/")
            if path == "admin/fault":
                if method == "POST":
                    kind = (body or {}).get("type")
                    if kind not in ("latency", "error"):
                        return 400, '{"error": "unknown fault kind"}'
                    p.faults[kind] = (float(body["magnitude"]), now + float(body.get("duration_s", 60)))
                elif method == "DELETE":
                    p.faults.clear()
                snapshot = {
                    k: {"magnitude": mag, "expires_in_s": round(exp - now, 1)}
                    for k, (mag, exp) in p.faults.items()
                    if exp > now
                }
                return 200, json.dumps({"pod": pod, "faults": snapshot})
            if path == "metrics":
                rss = 24e6 + 40e3 * min(p.requests, 2000) ** 0.5
                return 200, (
                    f"process_cpu_seconds_total {p.cpu_seconds:.6f}\nprocess_resident_memory_bytes {rss:.0f}\n"
                )
            return 404, '{"error": "not found"}'

    def endpoint_url(self, spec: ServiceSpec, variant: str) -> str:
        return f"sim://{spec.name}/{variant}"

    # ---------------------------------------------------------- data plane --
    def handle(self, service: str, variant: str = "live", _depth: int = 0) -> dict[str, Any]:
        """Serve one simulated ``GET /work``. Returns the outcome the gateway
        would have observed: status (None = transport error), latency, colour,
        and the error signature for failures."""
        with self._lock:
            now = self.clock()
            svc = self._svc(service)
            color = svc.live_color if variant == "live" else variant
            c = svc.colors[color]
            ready = [p for p in c.pods if p.ready(now)]
            if not ready:
                return {
                    "status": None,
                    "latency_ms": 1.0,
                    "color": color,
                    "pod": None,
                    "signature": f"ConnectionRefused:{m.deployment_name(service, color)}",
                }
            pod = self.rng.choice(ready)
            pod.requests += 1
            faults = pod.active_faults(now)
            env = c.env
            base = float(env.get("BASE_LATENCY_MS", svc.spec.base_latency_ms))
            workers = float(env.get("WORKERS", settings.demo_workers))
            rho = min(0.9, self.offered_rps.get(service, 0.0) * (base / 1000.0) / max(workers, 1.0))
            latency = base / (1.0 - rho) + self.rng.uniform(-3.0, 3.0)
            pod.cpu_seconds += base / 1000.0 * 0.2
            latency += float(env.get("FAULT_LATENCY_MS", 0.0)) + faults.get("latency", 0.0)
            error_rate = float(env.get("FAULT_ERROR_RATE", 0.0))

        out = {"status": 200, "latency_ms": latency, "color": color, "pod": pod.name, "signature": None}
        if (error_rate and self.rng.random() < error_rate) or self.rng.random() < faults.get("error", 0.0):
            return {**out, "status": 500, "signature": f"InternalError:{service}@{c.version}"}
        if _depth > 8:
            return out
        dep_timeout_ms = float(env.get("DEP_TIMEOUT_S", 2.0)) * 1000
        for dep in svc.spec.depends_on:
            if dep not in self.services:
                continue
            r = self.handle(dep, "live", _depth + 1)
            if r["status"] is not None and r["status"] < 500 and r["latency_ms"] <= dep_timeout_ms:
                out["latency_ms"] += r["latency_ms"]
                continue
            if r["status"] is None or r["latency_ms"] > dep_timeout_ms:
                timed_out = r["latency_ms"] > dep_timeout_ms
                sig = f"{'UpstreamTimeout' if timed_out else 'UpstreamUnavailable'}:{service}->{dep}"
                out["latency_ms"] += min(r["latency_ms"], dep_timeout_ms)
            else:
                sig = f"UpstreamError:{service}->{dep}:{r['status']}"
                out["latency_ms"] += r["latency_ms"]
            return {
                **out,
                "status": 502,
                "signature": sig,
                "caused_by": r.get("caused_by") or r.get("signature") or sig,
            }
        return out


def catalog_specs() -> list[ServiceSpec]:
    return list(catalog.CATALOG)
