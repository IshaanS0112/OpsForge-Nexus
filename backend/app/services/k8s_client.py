"""Cluster access: real Kubernetes API calls against the local kind cluster.

``KubernetesCluster`` wraps the official ``kubernetes`` Python client. Every
operation the deployment, canary and chaos engines perform is a real API
call:

* creating the Deployment/Service objects (server-side apply),
* rolling a colour to a new version (server-side apply of the Deployment),
* switching traffic (a PATCH of the live Service's selector),
* rollout status (read from the Deployment status and its pods — the same
  checks ``kubectl rollout status`` makes),
* fault injection (POST through the API server's pod proxy) and pod kills
  (DELETE of the pod; its ReplicaSet replaces it).

``SimulatedCluster`` (``sim_cluster.py``) implements the same interface in
memory for the test suite and for running without Docker; the API and the
dashboard always report which mode is active.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol

from app.catalog import ServiceSpec
from app.config import settings
from app.k8s import manifests as m

logger = logging.getLogger(__name__)

FIELD_MANAGER = "opsforge-nexus"
# Waiting reasons that will not resolve by waiting. Held for
# ``rollout_fast_fail_s`` they fail a rollout before the progress deadline.
FATAL_WAITING_REASONS = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "ErrImageNeverPull",
        "InvalidImageName",
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
    }
)


class ClusterError(RuntimeError):
    pass


@dataclass
class PodInfo:
    name: str
    color: Optional[str]
    version: Optional[str]
    phase: str
    ready: bool
    restarts: int = 0
    waiting_reason: Optional[str] = None
    terminating: bool = False
    node: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RolloutStatus:
    """Readiness of one colour's Deployment, computed like ``kubectl rollout
    status``: the spec change must be observed, every replica updated, no old
    replicas left, and every updated replica available."""

    service: str
    color: str
    version: Optional[str]
    desired: int
    updated: int
    ready: int
    available: int
    complete: bool
    failed: bool
    message: str
    fatal_reason: Optional[str] = None
    pods: list[PodInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pods"] = [p.to_dict() for p in self.pods]
        return d


def summarize_rollout(
    *,
    service: str,
    color: str,
    version: Optional[str],
    generation: int,
    observed_generation: int,
    desired: int,
    total: int,
    updated: int,
    ready: int,
    available: int,
    conditions: list[dict],
    pods: list[PodInfo],
) -> RolloutStatus:
    """Pure: map Deployment status fields to a verdict. ``conditions`` are
    ``{type, status, reason, message}`` dicts."""

    def status(complete: bool, failed: bool, message: str, fatal: Optional[str] = None) -> RolloutStatus:
        return RolloutStatus(
            service=service,
            color=color,
            version=version,
            desired=desired,
            updated=updated,
            ready=ready,
            available=available,
            complete=complete,
            failed=failed,
            message=message,
            fatal_reason=fatal,
            pods=pods,
        )

    for cond in conditions:
        if cond.get("type") == "Progressing" and cond.get("reason") == "ProgressDeadlineExceeded":
            return status(False, True, f"progress deadline exceeded: {cond.get('message') or ''}".strip())

    fatal = next((p.waiting_reason for p in pods if p.waiting_reason in FATAL_WAITING_REASONS), None)
    if generation > observed_generation:
        return status(False, False, "waiting for the spec change to be observed", fatal)
    if updated < desired:
        return status(False, False, f"{updated} of {desired} replicas updated", fatal)
    if total > updated:
        return status(False, False, f"{total - updated} old replicas pending termination", fatal)
    if available < updated:
        return status(False, False, f"{available} of {updated} updated replicas available", fatal)
    return status(True, False, f"{available}/{desired} replicas available")


def parse_prometheus_text(text: str) -> dict[str, float]:
    """Minimal exposition-format parser: ``name{labels} value`` -> {name: value}
    for unlabelled samples (the process metrics the demo pods expose)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "{" in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return out


class ClusterClient(Protocol):
    mode: str
    namespace: str

    def info(self) -> dict[str, Any]: ...
    def ensure_base(self) -> None: ...
    def ensure_service(self, spec: ServiceSpec) -> None: ...
    def live_color(self, service: str) -> Optional[str]: ...
    def switch_live(self, service: str, color: str) -> None: ...
    def apply_version(
        self,
        spec: ServiceSpec,
        color: str,
        version: str,
        *,
        config_diff: Optional[dict] = None,
        image: Optional[str] = None,
        replicas: Optional[int] = None,
    ) -> str: ...
    def scale(self, service: str, color: str, replicas: int) -> None: ...
    def rollout_status(self, service: str, color: str) -> RolloutStatus: ...
    def color_version(self, service: str, color: str) -> tuple[Optional[str], int]: ...
    def list_pods(self, service: str, color: Optional[str] = None) -> list[PodInfo]: ...
    def delete_pod(self, name: str) -> None: ...
    def pod_request(
        self, pod: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0
    ) -> tuple[int, str]: ...
    def endpoint_url(self, spec: ServiceSpec, variant: str) -> str: ...


def env_defaults() -> dict[str, str]:
    return {
        "STARTUP_DELAY_S": str(settings.demo_startup_delay_s),
        "WORKERS": str(settings.demo_workers),
        "CPU_WORK_ITERS": str(settings.demo_cpu_work_iters),
    }


class KubernetesCluster:
    """Real Kubernetes API client scoped to the OpsForge namespace."""

    mode = "kind"

    def __init__(
        self,
        namespace: str = settings.k8s_namespace,
        kubeconfig: str = settings.kubeconfig,
        context: str = settings.k8s_context,
        node_host: str = settings.cluster_node_host,
        image: str = settings.demo_service_image,
        replicas: int = settings.demo_replicas,
    ) -> None:
        from kubernetes import client, config

        self._client_mod = client
        cfg = client.Configuration()
        try:
            config.load_kube_config(config_file=kubeconfig or None, context=context or None, client_configuration=cfg)
            self._context = context or "current-context"
        except config.ConfigException:
            config.load_incluster_config(client_configuration=cfg)
            self._context = "in-cluster"
        self.namespace = namespace
        self.node_host = node_host
        self.image = image
        self.replicas = replicas
        self.api = client.ApiClient(cfg)
        self.core = client.CoreV1Api(self.api)
        self.apps = client.AppsV1Api(self.api)
        self._server = cfg.host
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers --
    def _exists(self, reader, *args) -> bool:
        from kubernetes.client.exceptions import ApiException

        try:
            reader(*args)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def _apply(self, patcher, name: str, body: dict) -> Any:
        """Server-side apply (``kubectl apply --server-side``)."""
        return patcher(
            name,
            self.namespace,
            body,
            field_manager=FIELD_MANAGER,
            force=True,
            _content_type="application/apply-patch+yaml",
        )

    # ---------------------------------------------------------------- info --
    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "context": self._context,
            "server": self._server,
            "namespace": self.namespace,
            "node_host": self.node_host,
        }
        try:
            version = self._client_mod.VersionApi(self.api).get_code()
            nodes = self.core.list_node().items
            out.update(
                reachable=True,
                server_version=version.git_version,
                nodes=[
                    {
                        "name": n.metadata.name,
                        "ready": any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or [])),
                        "kubelet": n.status.node_info.kubelet_version,
                    }
                    for n in nodes
                ],
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller, not raised
            out.update(reachable=False, error=str(exc))
        return out

    # ------------------------------------------------------------ topology --
    def ensure_base(self) -> None:
        if not self._exists(self.core.read_namespace, self.namespace):
            self.core.create_namespace(m.namespace_manifest(self.namespace))
            logger.info("created namespace %s", self.namespace)
        # Always re-apply so the pods' code matches this backend's version.
        self._apply(self.core.patch_namespaced_config_map, m.CODE_CONFIGMAP, m.code_configmap(self.namespace))

    def ensure_service(self, spec: ServiceSpec) -> None:
        """Create any missing objects for one service. Existing objects are
        left alone: they carry the live colour and deployed versions."""
        for variant in ("live", *m.COLORS):
            name = spec.name if variant == "live" else m.deployment_name(spec.name, variant)
            if not self._exists(self.core.read_namespaced_service, name, self.namespace):
                self.core.create_namespaced_service(self.namespace, m.service_manifest(self.namespace, spec, variant))
                logger.info("created service %s", name)
        for color in m.COLORS:
            name = m.deployment_name(spec.name, color)
            if not self._exists(self.apps.read_namespaced_deployment, name, self.namespace):
                body = m.deployment_manifest(
                    self.namespace,
                    spec,
                    color,
                    spec.initial_version,
                    replicas=self.replicas if color == "blue" else 0,
                    image=self.image,
                    env_defaults=env_defaults(),
                    progress_deadline_s=settings.rollout_timeout_s,
                )
                self.apps.create_namespaced_deployment(self.namespace, body)
                logger.info("created deployment %s", name)

    # ------------------------------------------------------------- traffic --
    def live_color(self, service: str) -> Optional[str]:
        svc = self.core.read_namespaced_service(service, self.namespace)
        return (svc.spec.selector or {}).get(m.COLOR_LABEL)

    def switch_live(self, service: str, color: str) -> None:
        """The blue-green switch: PATCH the live Service's selector."""
        self.core.patch_namespaced_service(service, self.namespace, {"spec": {"selector": m.selector(service, color)}})
        logger.info("service %s selector -> %s", service, color)

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
        name = m.deployment_name(spec.name, color)
        body = m.deployment_manifest(
            self.namespace,
            spec,
            color,
            version,
            replicas=self.replicas if replicas is None else replicas,
            image=image or self.image,
            env_overrides=m.applied_env(config_diff),
            env_defaults=env_defaults(),
            progress_deadline_s=settings.rollout_timeout_s,
        )
        self._apply(self.apps.patch_namespaced_deployment, name, body)
        logger.info("applied %s %s -> %s", name, version, image or self.image)
        return name

    def scale(self, service: str, color: str, replicas: int) -> None:
        self.apps.patch_namespaced_deployment_scale(
            m.deployment_name(service, color), self.namespace, {"spec": {"replicas": replicas}}
        )

    def color_version(self, service: str, color: str) -> tuple[Optional[str], int]:
        dep = self.apps.read_namespaced_deployment(m.deployment_name(service, color), self.namespace)
        labels = dep.spec.template.metadata.labels or {}
        return labels.get(m.VERSION_LABEL), dep.spec.replicas or 0

    def rollout_status(self, service: str, color: str) -> RolloutStatus:
        dep = self.apps.read_namespaced_deployment(m.deployment_name(service, color), self.namespace)
        st = dep.status
        labels = dep.spec.template.metadata.labels or {}
        version = labels.get(m.VERSION_LABEL)
        # Only pods of the current template count toward readiness.
        pods = [p for p in self.list_pods(service, color) if p.version == version and not p.terminating]
        return summarize_rollout(
            service=service,
            color=color,
            version=version,
            generation=dep.metadata.generation or 0,
            observed_generation=st.observed_generation or 0,
            desired=dep.spec.replicas or 0,
            total=st.replicas or 0,
            updated=st.updated_replicas or 0,
            ready=st.ready_replicas or 0,
            available=st.available_replicas or 0,
            conditions=[
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (st.conditions or [])
            ],
            pods=pods,
        )

    # ---------------------------------------------------------------- pods --
    def list_pods(self, service: str, color: Optional[str] = None) -> list[PodInfo]:
        selector = f"{m.APP_LABEL}={service}"
        if color:
            selector += f",{m.COLOR_LABEL}={color}"
        pods = self.core.list_namespaced_pod(self.namespace, label_selector=selector).items
        out = []
        for p in pods:
            statuses = p.status.container_statuses or []
            waiting = next(
                (s.state.waiting.reason for s in statuses if s.state and s.state.waiting and s.state.waiting.reason),
                None,
            )
            labels = p.metadata.labels or {}
            out.append(
                PodInfo(
                    name=p.metadata.name,
                    color=labels.get(m.COLOR_LABEL),
                    version=labels.get(m.VERSION_LABEL),
                    phase=p.status.phase or "Unknown",
                    ready=any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])),
                    restarts=sum(s.restart_count or 0 for s in statuses),
                    waiting_reason=waiting,
                    terminating=p.metadata.deletion_timestamp is not None,
                    node=p.spec.node_name,
                )
            )
        return sorted(out, key=lambda x: x.name)

    def delete_pod(self, name: str) -> None:
        self.core.delete_namespaced_pod(name, self.namespace, grace_period_seconds=0)
        logger.info("deleted pod %s", name)

    def pod_request(
        self, pod: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0
    ) -> tuple[int, str]:
        """Call a pod's HTTP endpoint through the API server's pod proxy
        (``/api/v1/namespaces/{ns}/pods/{pod}:8080/proxy/{path}``)."""
        from kubernetes.client.exceptions import ApiException

        resource = f"/api/v1/namespaces/{self.namespace}/pods/{pod}:{m.CONTAINER_PORT}/proxy/{path.lstrip('/')}"
        try:
            resp = self.api.call_api(
                resource,
                method,
                header_params={"Content-Type": "application/json", "Accept": "*/*"},
                body=body,  # the REST layer JSON-encodes dicts itself
                auth_settings=["BearerToken"],
                _preload_content=False,
                _return_http_data_only=True,
                _request_timeout=timeout,
            )
            return resp.status, resp.data.decode()
        except ApiException as exc:
            return exc.status or 0, exc.body.decode() if isinstance(exc.body, bytes) else str(exc.body or exc)

    def endpoint_url(self, spec: ServiceSpec, variant: str) -> str:
        return f"http://{self.node_host}:{m.node_port(spec, variant)}"


def build_cluster(mode: Optional[str] = None) -> ClusterClient:
    mode = (mode or settings.cluster_mode).lower()
    if mode in ("kind", "kubernetes", "k8s"):
        return KubernetesCluster()
    if mode == "simulated":
        from app.services.sim_cluster import SimulatedCluster

        return SimulatedCluster()
    raise ClusterError(f"unknown cluster mode {mode!r} (expected 'kind' or 'simulated')")
