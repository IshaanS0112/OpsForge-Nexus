"""Manifest templates, rollout-status logic, and the simulated cluster."""

import json

import pytest

from app import catalog
from app.k8s import manifests as m
from app.services.k8s_client import PodInfo, parse_prometheus_text, summarize_rollout
from app.services.sim_cluster import SimulatedCluster


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ---------------------------------------------------------------- manifests --
def test_node_ports_are_unique_and_in_range():
    ports = [m.node_port(s, v) for s in catalog.CATALOG for v in ("live", "blue", "green")]
    assert len(ports) == len(set(ports))
    assert all(30000 <= p <= 32767 for p in ports)


def test_live_service_selects_one_color():
    spec = catalog.get("payments-svc")
    live = m.service_manifest("opsforge", spec, "live", live_color="green")
    assert live["metadata"]["name"] == "payments-svc"
    assert live["spec"]["selector"] == {m.APP_LABEL: "payments-svc", m.COLOR_LABEL: "green"}
    pinned = m.service_manifest("opsforge", spec, "blue", live_color="green")
    assert pinned["metadata"]["name"] == "payments-svc-blue"
    assert pinned["spec"]["selector"][m.COLOR_LABEL] == "blue"


def test_deployment_manifest_wires_dependencies_and_version():
    spec = catalog.get("checkout-api")
    d = m.deployment_manifest("opsforge", spec, "green", "v2.0.0", replicas=1, image="python:3.12-alpine")
    tmpl = d["spec"]["template"]
    env = {e["name"]: e["value"] for e in tmpl["spec"]["containers"][0]["env"]}
    assert env["DEPENDENCIES"] == "payments-svc,inventory-svc"
    assert env["APP_VERSION"] == "v2.0.0" and env["COLOR"] == "green"
    assert tmpl["metadata"]["labels"][m.VERSION_LABEL] == "v2.0.0"
    # the selector must not include the version, or it could never change
    assert m.VERSION_LABEL not in d["spec"]["selector"]["matchLabels"]


def test_only_allowlisted_config_keys_become_env():
    applied = m.applied_env({"FAULT_ERROR_RATE": 0.3, "flag": "new_pricing", "WORKERS": 8})
    assert applied == {"FAULT_ERROR_RATE": "0.3", "WORKERS": "8"}


def test_config_change_changes_pod_template_hash():
    spec = catalog.get("ledger-svc")
    a = m.deployment_manifest("ns", spec, "blue", "v1", replicas=1, image="img")
    b = m.deployment_manifest(
        "ns", spec, "blue", "v1", replicas=1, image="img", env_overrides={"FAULT_ERROR_RATE": "0.2"}
    )
    ha = a["spec"]["template"]["metadata"]["annotations"]["opsforge.io/config-hash"]
    hb = b["spec"]["template"]["metadata"]["annotations"]["opsforge.io/config-hash"]
    assert ha != hb


# ----------------------------------------------------------- rollout status --
def _rollout(**kw):
    base = dict(
        service="s",
        color="green",
        version="v2",
        generation=2,
        observed_generation=2,
        desired=1,
        total=1,
        updated=1,
        ready=1,
        available=1,
        conditions=[],
        pods=[],
    )
    base.update(kw)
    return summarize_rollout(**base)


def test_rollout_complete_when_all_updated_and_available():
    st = _rollout()
    assert st.complete and not st.failed


@pytest.mark.parametrize(
    "kw, fragment",
    [
        ({"observed_generation": 1}, "observed"),
        ({"updated": 0, "available": 0}, "updated"),
        ({"total": 2}, "old replicas"),
        ({"available": 0}, "available"),
    ],
)
def test_rollout_waits_like_kubectl(kw, fragment):
    st = _rollout(**kw)
    assert not st.complete and not st.failed
    assert fragment in st.message


def test_progress_deadline_fails_rollout():
    st = _rollout(
        available=0,
        conditions=[
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded", "message": "timed out"}
        ],
    )
    assert st.failed and "deadline" in st.message


def test_fatal_pod_reason_is_reported_while_waiting():
    pod = PodInfo(
        name="p", color="green", version="v2", phase="Pending", ready=False, waiting_reason="ImagePullBackOff"
    )
    st = _rollout(available=0, pods=[pod])
    assert not st.complete and st.fatal_reason == "ImagePullBackOff"


def test_parse_prometheus_text_skips_labelled_and_comments():
    text = (
        "# TYPE x counter\n"
        "process_cpu_seconds_total 1.5\n"
        'opsforge_requests_total{code="200"} 9\n'
        "process_resident_memory_bytes 2.4e7\n"
    )
    assert parse_prometheus_text(text) == {"process_cpu_seconds_total": 1.5, "process_resident_memory_bytes": 2.4e7}


# --------------------------------------------------------- simulated cluster --
@pytest.fixture()
def sim():
    clock = Clock()
    c = SimulatedCluster(clock=clock, startup_delay_s=10.0, replicas=1)
    for spec in catalog.CATALOG:
        c.ensure_service(spec)
    c.clock_ref = clock
    return c


def test_sim_pods_warm_up_before_ready(sim):
    assert not sim.rollout_status("payments-svc", "blue").complete
    sim.clock_ref.advance(10)
    assert sim.rollout_status("payments-svc", "blue").complete


def test_sim_bad_image_never_becomes_ready(sim):
    spec = catalog.get("payments-svc")
    sim.apply_version(spec, "green", "v9", image="python:does-not-exist")
    sim.clock_ref.advance(60)
    st = sim.rollout_status("payments-svc", "green")
    assert not st.complete and st.fatal_reason == "ErrImagePull"


def test_sim_selector_switch_moves_live_traffic(sim):
    sim.clock_ref.advance(10)
    spec = catalog.get("ledger-svc")
    sim.apply_version(spec, "green", "v2.0.0")
    sim.clock_ref.advance(10)
    assert sim.handle("ledger-svc")["color"] == "blue"
    sim.switch_live("ledger-svc", "green")
    assert sim.handle("ledger-svc")["color"] == "green"


def test_sim_fault_cascades_to_dependents_with_root_signature(sim):
    sim.clock_ref.advance(10)
    ledger_pod = sim.list_pods("ledger-svc", "blue")[0].name
    code, body = sim.pod_request(
        ledger_pod, "POST", "admin/fault", {"type": "error", "magnitude": 1.0, "duration_s": 30}
    )
    assert code == 200 and "error" in json.loads(body)["faults"]

    payments = sim.handle("payments-svc")
    checkout = sim.handle("checkout-api")
    inventory = sim.handle("inventory-svc")
    assert payments["status"] == 502 and payments["signature"] == "UpstreamError:payments-svc->ledger-svc:500"
    assert checkout["status"] == 502 and checkout["caused_by"] == "ChaosErrorBurst:ledger-svc"
    assert inventory["status"] == 200  # not downstream of ledger

    sim.clock_ref.advance(31)  # fault TTL expires on its own
    assert sim.handle("payments-svc")["status"] == 200


def test_sim_pod_kill_leaves_gap_until_replacement_is_ready(sim):
    sim.clock_ref.advance(10)
    pod = sim.list_pods("catalog-svc", "blue")[0].name
    sim.delete_pod(pod)
    assert sim.handle("catalog-svc")["status"] is None  # no endpoints
    assert sim.handle("inventory-svc")["status"] == 502
    sim.clock_ref.advance(10)
    assert sim.handle("catalog-svc")["status"] == 200
    assert sim.list_pods("catalog-svc")[0].name != pod
