"""Manifest templates for the objects OpsForge owns in the cluster.

Per catalog service:

* ``Deployment {svc}-blue`` and ``Deployment {svc}-green`` — one per colour.
* ``Service {svc}`` — the *live* Service. Its selector names one colour;
  switching traffic is a PATCH of that selector. Other services reach this
  one through it (cluster DNS), so east-west traffic follows the switch.
* ``Service {svc}-blue`` / ``{svc}-green`` — address one colour directly. The
  gateway uses these to split edge traffic during a canary.

All Services are NodePort so the gateway (outside the cluster) can reach
them; ports come from a fixed block so the kind config can map them to the
host.

Builders return plain dicts, which the Kubernetes API accepts as-is and which
tests can assert on without a cluster.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.catalog import ServiceSpec

COLORS = ("blue", "green")
CONTAINER_NAME = "app"
CONTAINER_PORT = 8080
NODE_PORT_BASE = 30100
CODE_CONFIGMAP = "opsforge-demo-service"
# Container limits; the telemetry scraper normalises CPU and memory against them.
CPU_LIMIT_CORES = 0.5
MEMORY_LIMIT_BYTES = 128 * 1024 * 1024
CODE_MOUNT = "/opt/opsforge"

APP_LABEL = "app.kubernetes.io/name"
COLOR_LABEL = "opsforge.io/color"
VERSION_LABEL = "opsforge.io/version"
CODE_HASH_ANNOTATION = "opsforge.io/code-hash"
COMMON_LABELS = {
    "app.kubernetes.io/part-of": "opsforge-nexus",
    "app.kubernetes.io/managed-by": "opsforge-nexus",
}

# config_diff keys that become container environment. Anything else in a
# config_diff is kept as release metadata (and shown to RCA) but not applied.
APPLIED_CONFIG_KEYS = frozenset(
    {
        "FAULT_ERROR_RATE",
        "FAULT_LATENCY_MS",
        "BASE_LATENCY_MS",
        "LATENCY_JITTER_MS",
        "WORKERS",
        "CPU_WORK_ITERS",
        "STARTUP_DELAY_S",
        "DEP_TIMEOUT_S",
        "QUEUE_TIMEOUT_S",
    }
)


def other_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def deployment_name(service: str, color: str) -> str:
    return f"{service}-{color}"


def node_port(spec: ServiceSpec, variant: str) -> int:
    """variant: 'live' | 'blue' | 'green'."""
    offset = {"live": 0, "blue": 1, "green": 2}[variant]
    return NODE_PORT_BASE + spec.port_index * 3 + offset


def demo_service_source() -> str:
    return (Path(__file__).parent / "demo_service.py").read_text()


def code_hash(source: str | None = None) -> str:
    """Fingerprint of the workload code the ConfigMap ships. It is stamped on
    every pod template: a pod only picks up new code when it restarts, so a
    changed hash is what rolls the pods (see KubernetesCluster.ensure_service)."""
    return hashlib.sha256((source if source is not None else demo_service_source()).encode()).hexdigest()[:16]


def drift_patch(annotations: dict | None, env: dict[str, str], code: str, defaults: dict[str, str]) -> dict | None:
    """The pod-template patch that brings an existing Deployment back in line
    with this backend: the workload code the ConfigMap now ships, and the
    operational env defaults from settings. None when it already matches.

    A release's own config (a version's ``config_diff``) is left alone: the
    container's env list merges by name, so only the drifting keys are set.
    Applying it rolls the pods, which is what picking up new code or a new
    readiness delay requires."""
    template: dict = {}
    if (annotations or {}).get(CODE_HASH_ANNOTATION) != code:
        template["metadata"] = {"annotations": {CODE_HASH_ANNOTATION: code}}
    drift = {k: v for k, v in defaults.items() if env.get(k) != v}
    if drift:
        template["spec"] = {
            "containers": [{"name": CONTAINER_NAME, "env": [{"name": k, "value": v} for k, v in sorted(drift.items())]}]
        }
    return {"spec": {"template": template}} if template else None


def selector(service: str, color: str) -> dict[str, str]:
    return {APP_LABEL: service, COLOR_LABEL: color}


def namespace_manifest(namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": dict(COMMON_LABELS)},
    }


def code_configmap(namespace: str, source: str | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CODE_CONFIGMAP, "namespace": namespace, "labels": dict(COMMON_LABELS)},
        "data": {"app.py": source if source is not None else demo_service_source()},
    }


def applied_env(config_diff: dict | None) -> dict[str, str]:
    """The subset of a config_diff that is applied as container env."""
    return {
        k: str(v)
        for k, v in (config_diff or {}).items()
        if k in APPLIED_CONFIG_KEYS and isinstance(v, (int, float, str))
    }


def container_env(
    spec: ServiceSpec, color: str, version: str, overrides: dict[str, str], defaults: dict[str, str]
) -> list[dict]:
    env = {
        "SERVICE_NAME": spec.name,
        "APP_VERSION": version,
        "COLOR": color,
        "DEPENDENCIES": ",".join(spec.depends_on),
        "BASE_LATENCY_MS": str(spec.base_latency_ms),
        "PYTHONUNBUFFERED": "1",
        **defaults,
        **overrides,
    }
    return [{"name": k, "value": v} for k, v in sorted(env.items())]


def deployment_manifest(
    namespace: str,
    spec: ServiceSpec,
    color: str,
    version: str,
    *,
    replicas: int,
    image: str,
    env_overrides: dict[str, str] | None = None,
    env_defaults: dict[str, str] | None = None,
    progress_deadline_s: int = 120,
) -> dict:
    env = container_env(spec, color, version, env_overrides or {}, env_defaults or {})
    labels = {**COMMON_LABELS, **selector(spec.name, color), VERSION_LABEL: version}
    # Changing only env/image must still roll the pods; the hash makes the
    # template differ whenever the effective configuration does.
    config_hash = hashlib.sha256(json.dumps([image, env], sort_keys=True).encode()).hexdigest()[:16]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": deployment_name(spec.name, color),
            "namespace": namespace,
            "labels": labels,
            "annotations": {"opsforge.io/version": version},
        },
        "spec": {
            "replicas": replicas,
            "progressDeadlineSeconds": progress_deadline_s,
            "revisionHistoryLimit": 3,
            "selector": {"matchLabels": selector(spec.name, color)},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {"opsforge.io/config-hash": config_hash, CODE_HASH_ANNOTATION: code_hash()},
                },
                "spec": {
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 5,
                    "containers": [
                        {
                            "name": CONTAINER_NAME,
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", f"{CODE_MOUNT}/app.py"],
                            "ports": [{"name": "http", "containerPort": CONTAINER_PORT}],
                            "env": env,
                            "readinessProbe": {
                                "httpGet": {"path": "/readyz", "port": CONTAINER_PORT},
                                "periodSeconds": 2,
                                "failureThreshold": 2,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": CONTAINER_PORT},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "32Mi"},
                                "limits": {
                                    "cpu": f"{int(CPU_LIMIT_CORES * 1000)}m",
                                    "memory": f"{MEMORY_LIMIT_BYTES // (1024 * 1024)}Mi",
                                },
                            },
                            "volumeMounts": [{"name": "code", "mountPath": CODE_MOUNT, "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "code", "configMap": {"name": CODE_CONFIGMAP}}],
                },
            },
        },
    }


def service_manifest(namespace: str, spec: ServiceSpec, variant: str, live_color: str = "blue") -> dict:
    """variant 'live' -> Service ``{svc}`` selecting ``live_color``;
    'blue'/'green' -> Service ``{svc}-{color}`` pinned to that colour."""
    color = live_color if variant == "live" else variant
    name = spec.name if variant == "live" else deployment_name(spec.name, variant)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {**COMMON_LABELS, APP_LABEL: spec.name, "opsforge.io/variant": variant},
        },
        "spec": {
            "type": "NodePort",
            "selector": selector(spec.name, color),
            "ports": [
                {
                    "name": "http",
                    "port": CONTAINER_PORT,
                    "targetPort": CONTAINER_PORT,
                    "nodePort": node_port(spec, variant),
                }
            ],
        },
    }
