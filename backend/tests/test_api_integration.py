"""API surface: validation, conflicts, manual promotion, live k8s status,
services/graph/SLO endpoints, metric ingestion, RCA + impact on a real
rollback incident, and the console snapshot."""

import random

from helpers import drive, status, warm

from app.models import Deployment


def test_health_endpoint(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["version"] == "3.0.0"


def test_deploy_validation_and_conflicts(client, runtime, clock, db):
    assert client.post("/deployments", json={"service_name": "web", "version": "v1"}).status_code == 404
    assert client.post("/deployments", json={"service_name": "ledger-svc", "version": "v1/../2"}).status_code == 422
    r = client.post("/deployments", json={"service_name": "ledger-svc", "version": "v1.1.0"})
    assert r.status_code == 202 and r.json()["status"] == "IDLE"
    assert r.json()["target_color"] == "green" and r.json()["stable_color"] == "blue"
    again = client.post("/deployments", json={"service_name": "ledger-svc", "version": "v1.2.0"})
    assert again.status_code == 409  # one rollout per service at a time


def test_manual_promotion_rechecks_the_gate(client, runtime, clock, db):
    runtime.routing._rng = random.Random(5)
    warm(runtime, clock, 6)
    dep_id = client.post(
        "/deployments", json={"service_name": "ledger-svc", "version": "v1.1.0", "auto_promote": False}
    ).json()["id"]
    drive(runtime, clock, 6, until=lambda: status(db, dep_id) == "CANARY_5")
    early = client.post(f"/deployments/{dep_id}/promote-canary")
    assert early.status_code == 409 and early.json()["detail"]["action"] == "wait"  # not enough canary traffic yet

    drive(runtime, clock, 12)  # the passing stage now holds for an operator
    assert status(db, dep_id) == "CANARY_5"
    assert "awaiting manual promotion" in db.get(Deployment, dep_id).status_reason
    ok = client.post(f"/deployments/{dep_id}/promote-canary")
    assert ok.status_code == 200, ok.text
    assert ok.json()["deployment"]["status"] == "CANARY_25"
    assert ok.json()["deployment"]["canary_weight"] == 25


def test_k8s_status_reports_both_colours(client, runtime, clock, db):
    warm(runtime, clock, 2)
    dep_id = client.post("/deployments", json={"service_name": "ledger-svc", "version": "v1.1.0"}).json()["id"]
    drive(runtime, clock, 1)
    body = client.get(f"/deployments/{dep_id}/k8s-status").json()
    assert body["cluster_mode"] == "simulated" and body["live_color"] == "blue"
    assert body["candidate"]["version"] == "v1.1.0" and body["stable"]["complete"] is True
    assert body["candidate"]["pods"]


def test_manual_rollback_restores_stable(client, runtime, clock, db):
    warm(runtime, clock, 2)
    dep_id = client.post(
        "/deployments", json={"service_name": "ledger-svc", "version": "v1.1.0", "strategy": "BLUE_GREEN"}
    ).json()["id"]
    drive(runtime, clock, 6, until=lambda: status(db, dep_id) == "LIVE")
    assert runtime.cluster.live_color("ledger-svc") == "green"
    r = client.post(f"/deployments/{dep_id}/rollback")
    assert r.status_code == 200 and r.json()["status"] == "ROLLED_BACK"
    assert runtime.cluster.live_color("ledger-svc") == "blue"
    assert not runtime.rollback.in_cooldown("ledger-svc")  # manual rollbacks do not start a cooldown
    assert client.post(f"/deployments/{dep_id}/rollback").status_code == 409


def test_service_graph_endpoints(client):
    deps = client.get("/services/checkout-api/dependencies").json()
    assert deps["upstream"] == ["inventory-svc", "payments-svc"]
    assert deps["downstream"] == ["notification-svc"]
    radius = client.get("/services/ledger-svc/blast-radius").json()
    assert radius["blast_radius"] == ["payments-svc", "checkout-api", "notification-svc"]
    # the graph must stay acyclic
    cyc = client.post(
        "/services/graph/edges", json={"service_name": "ledger-svc", "depends_on_service": "notification-svc"}
    )
    assert cyc.status_code == 422 and "cycle" in cyc.json()["detail"]
    assert client.get("/services/nope/blast-radius").status_code == 404


def test_slo_config_and_burn_rate(client, runtime, clock):
    warm(runtime, clock, 4)
    r = client.put("/services/ledger-svc/slo", json={"slo_target": 0.995, "latency_threshold_ms": 200})
    assert r.status_code == 200 and r.json()["slo_target"] == 0.995
    burn = client.get("/services/ledger-svc/slo-burn-rate").json()
    assert burn["slo_target"] == 0.995
    assert [e["severity"] for e in burn["evaluations"]] == ["FAST_BURN", "MEDIUM_BURN", "SLOW_BURN"]


def test_anomaly_consensus_endpoint(client, runtime, clock):
    warm(runtime, clock, 4)
    body = client.get("/services/ledger-svc/anomaly-consensus").json()
    latest = body["latest"]
    assert set(latest["layers"]) == {"zscore", "seasonal", "isolation_forest"}
    assert latest["agreeing_layers"] == 0


def test_single_metric_ingest_returns_zscore_but_never_acts(client, db):
    for i in range(20):
        v = 0.010 + (0.001 if i % 2 == 0 else -0.001)
        client.post("/metrics/ingest", json={"service_name": "api", "metric_name": "error_rate", "value": v})
    first = client.post("/metrics/ingest", json={"service_name": "api", "metric_name": "error_rate", "value": 0.5})
    assert first.status_code == 201 and first.json()["z_score"] > 3
    for _ in range(3):
        client.post("/metrics/ingest", json={"service_name": "api", "metric_name": "error_rate", "value": 0.5})
    assert client.get("/incidents").json() == []  # one layer never auto-acts


def test_batch_ingest_runs_the_suite(client):
    r = client.post(
        "/metrics/ingest-batch",
        json={
            "service_name": "external-api",
            "error_rate": 0.01,
            "latency_p95": 120,
            "cpu_usage": 0.3,
            "memory_usage": 0.4,
        },
    )
    assert r.status_code == 201
    assert set(r.json()["layers"]) == {"zscore", "seasonal", "isolation_forest"}


def test_rollback_incident_gets_rca_and_impact(client, runtime, clock, db):
    runtime.routing._rng = random.Random(3)
    warm(runtime, clock, 12)
    dep_id = client.post(
        "/deployments", json={"service_name": "payments-svc", "version": "v2.0.0", "simulate_failure": True}
    ).json()["id"]
    drive(runtime, clock, 40, until=lambda: status(db, dep_id) == "ROLLED_BACK")
    incident = client.get("/incidents").json()[0]
    assert incident["related_deployment_id"] == dep_id

    rca = client.post(f"/incidents/{incident['id']}/trigger-rca")
    assert rca.status_code == 200
    body = rca.json()
    assert body["llm_model_used"] == "rule-based-fallback"  # no API key in tests
    assert "v2.0.0" in body["root_cause_candidates"][0]["cause"]
    assert body["signals"]["recent_deployment"]["version"] == "v2.0.0"

    impact = client.get(f"/incidents/{incident['id']}/business-impact").json()
    assert "formula" in impact["calculation_basis"]
    assert impact["affected_requests"] > 0


def test_console_snapshot_shape(client, runtime, clock):
    warm(runtime, clock, 3)
    snap = client.get("/console/snapshot").json()
    assert snap["cluster"]["mode"] == "simulated"
    assert snap["summary"]["services"] == 6 and snap["summary"]["open_incidents"] == 0
    names = [s["name"] for s in snap["services"]]
    assert names.index("ledger-svc") < names.index("payments-svc")  # dependencies first
    svc = next(s for s in snap["services"] if s["name"] == "checkout-api")
    assert svc["status"] == "ok" and svc["values"]["latency_p95"] > 0
    assert {"service": "checkout-api", "depends_on": "payments-svc"} in snap["edges"]
    info = client.get("/cluster").json()
    assert info["mode"] == "simulated" and info["bootstrapped"] is True
