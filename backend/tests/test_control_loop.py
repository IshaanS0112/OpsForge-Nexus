"""End-to-end control-loop scenarios on the simulated cluster: rollouts,
canary gates, detection, cascades, chaos drills and rollbacks."""

import random

import pytest
from helpers import drive, status, warm

from app.models import ChaosExperiment, Deployment, Incident
from app.services.deployment_engine import CANARY_STATES


@pytest.fixture()
def rt(runtime):
    runtime.routing._rng = random.Random(3)
    return runtime


def _deploy(client, **body):
    r = client.post("/deployments", json={"service_name": "payments-svc", "version": "v1.1.0", **body})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_healthy_canary_walks_every_stage_to_live(client, rt, clock, db):
    warm(rt, clock, 12)
    dep_id = _deploy(client)
    drive(rt, clock, 80, until=lambda: status(db, dep_id) == "LIVE")

    dep = db.get(Deployment, dep_id)
    assert dep.status == "LIVE"
    stages = [h["stage"] for h in dep.stage_history]
    for s in ("DEPLOYING_GREEN", "HEALTH_CHECKING", "CANARY_5", "CANARY_25", "CANARY_50", "CANARY_100", "LIVE"):
        assert s in stages
    gates = [h["gate"]["verdict"] for h in dep.stage_history if "gate" in h]
    assert gates and all(v == "PASS" for v in gates)
    # the blue-green switch really happened: live Service selector moved
    assert rt.cluster.live_color("payments-svc") == dep.target_color == "green"
    assert rt.routing.route("payments-svc").live_color == "green"
    assert db.query(Incident).count() == 0


def test_failing_canary_is_rolled_back_by_its_burn_rate_gate(client, rt, clock, db):
    warm(rt, clock, 12)
    dep_id = _deploy(client, simulate_failure=True)
    drive(rt, clock, 40, until=lambda: status(db, dep_id) == "ROLLED_BACK")

    dep = db.get(Deployment, dep_id)
    assert dep.status == "ROLLED_BACK"
    failed = next(h for h in dep.stage_history if h["stage"] == "ROLLING_BACK")
    assert "canary gate failed" in failed["reason"]
    assert failed["gate"]["verdict"] == "FAIL"
    # it never got past the early stages, and 100% of traffic is back on stable
    assert "CANARY_50" not in [h["stage"] for h in dep.stage_history]
    assert rt.routing.route("payments-svc").canary_color is None
    assert rt.cluster.live_color("payments-svc") == "blue"
    assert rt.cluster.color_version("payments-svc", "green")[1] == 0  # candidate scaled to zero
    inc = db.query(Incident).filter_by(related_deployment_id=dep_id).one()
    assert inc.severity == "HIGH" and "canary gate" in inc.trigger_reason
    assert inc.detection_context["release_specific"] is True  # stable colour was healthy
    assert rt.rollback.in_cooldown(db, "payments-svc", clock.now)


def test_blue_green_switches_after_health_gate(client, rt, clock, db):
    warm(rt, clock, 6)
    dep_id = _deploy(client, strategy="BLUE_GREEN")
    drive(rt, clock, 10, until=lambda: status(db, dep_id) == "LIVE")
    dep = db.get(Deployment, dep_id)
    stages = [h["stage"] for h in dep.stage_history]
    assert dep.status == "LIVE" and not any(s.startswith("CANARY") for s in stages)
    assert rt.cluster.live_color("payments-svc") == "green"


def test_unpullable_image_fails_the_rollout(client, rt, clock, db):
    warm(rt, clock, 4)
    dep_id = _deploy(client, image="python:3.12-alpine-does-not-exist")
    drive(rt, clock, 12, until=lambda: status(db, dep_id) == "ROLLED_BACK")
    dep = db.get(Deployment, dep_id)
    assert dep.status == "ROLLED_BACK"
    assert "ErrImagePull" in next(h for h in dep.stage_history if h["stage"] == "ROLLING_BACK")["reason"]
    assert rt.cluster.live_color("payments-svc") == "blue"
    inc = db.query(Incident).filter_by(related_deployment_id=dep_id).one()
    assert inc.detection_context["release_specific"] is True  # it never served a request


def test_chaos_error_burst_cascades_and_drill_passes(client, rt, clock, db):
    warm(rt, clock)
    r = client.post("/chaos/inject", json={"service_name": "ledger-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0})
    assert r.status_code == 201, r.text
    exp_id = r.json()["id"]
    drive(rt, clock, 30, until=lambda: db.get(ChaosExperiment, exp_id).status == "COMPLETED")

    db.expire_all()
    exp = db.get(ChaosExperiment, exp_id)
    assert exp.status == "COMPLETED" and exp.passed, exp.notes
    assert exp.detection_latency_seconds <= 20  # K=3 ticks of 5 s
    assert exp.recovery_latency_seconds is not None

    incidents = {i.service_name: i for i in db.query(Incident).all()}
    root = incidents["ledger-svc"]
    assert not root.is_cascading and root.chaos_experiment_id == exp_id
    for victim in ("payments-svc", "checkout-api", "notification-svc"):
        assert incidents[victim].is_cascading and incidents[victim].root_incident_id == root.id
    assert "inventory-svc" not in incidents  # not downstream of ledger
    assert set(root.blast_radius_services) == {"payments-svc", "checkout-api", "notification-svc"}

    tree = client.get(f"/incidents/{incidents['checkout-api'].id}/cascade-tree").json()
    assert tree["root"]["service_name"] == "ledger-svc"
    assert {"parent": "payments-svc", "child": "checkout-api"} in tree["edges"]


def test_canary_targeted_drill_measures_detection_and_rollback(client, rt, clock, db):
    warm(rt, clock, 12)
    dep_id = _deploy(client, auto_promote=False)
    drive(rt, clock, 10, until=lambda: status(db, dep_id) in {s.value for s in CANARY_STATES})
    r = client.post(
        "/chaos/inject",
        json={"service_name": "payments-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0, "target": "canary"},
    )
    assert r.status_code == 201, r.text
    exp_id = r.json()["id"]
    assert r.json()["deployment_id"] == dep_id
    drive(rt, clock, 30, until=lambda: db.get(ChaosExperiment, exp_id).status == "COMPLETED")

    db.expire_all()
    exp = db.get(ChaosExperiment, exp_id)
    assert status(db, dep_id) == "ROLLED_BACK"
    assert exp.passed, exp.notes
    assert exp.rollback_latency_seconds is not None and exp.rollback_latency_seconds <= 90
    assert exp.detection_latency_seconds <= exp.rollback_latency_seconds
    assert any("detected by" in e["event"] for e in exp.timeline)
    # the rollback's incident is linked to the drill that caused it
    inc = db.query(Incident).filter_by(related_deployment_id=dep_id).one()
    assert inc.chaos_experiment_id == exp_id


def test_live_release_rolled_back_on_fast_burn_with_consensus(client, rt, clock, db):
    warm(rt, clock)
    dep_id = _deploy(client, strategy="BLUE_GREEN")
    drive(rt, clock, 10, until=lambda: status(db, dep_id) == "LIVE")
    drive(rt, clock, 6)
    # break the new (now live) colour; its dependencies stay healthy
    r = client.post(
        "/chaos/inject",
        json={"service_name": "payments-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0, "target": "stable"},
    )
    assert r.status_code == 201, r.text
    drive(rt, clock, 12, until=lambda: status(db, dep_id) == "ROLLED_BACK")

    dep = db.get(Deployment, dep_id)
    assert dep.status == "ROLLED_BACK"
    assert "FAST_BURN" in next(h for h in dep.stage_history if h["stage"] == "ROLLING_BACK")["reason"]
    assert rt.cluster.live_color("payments-svc") == "blue"
    inc = db.query(Incident).filter_by(service_name="payments-svc").one()
    assert inc.severity == "CRITICAL"


def test_cascade_victim_release_is_not_rolled_back(client, rt, clock, db):
    warm(rt, clock)
    r = client.post(
        "/deployments", json={"service_name": "checkout-api", "version": "v2.0.0", "strategy": "BLUE_GREEN"}
    )
    dep_id = r.json()["id"]
    drive(rt, clock, 10, until=lambda: status(db, dep_id) == "LIVE")
    drive(rt, clock, 6)
    client.post(
        "/chaos/inject",
        json={"service_name": "ledger-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0, "duration_s": 60},
    )
    drive(rt, clock, 10)

    assert status(db, dep_id) == "LIVE"  # checkout's release is not what broke
    checkout = db.query(Incident).filter_by(service_name="checkout-api").one()
    assert checkout.is_cascading
    assert rt.burns["checkout-api"].worst == "FAST_BURN"


def test_incidents_resolve_after_quiet_period(client, rt, clock, db):
    warm(rt, clock)
    client.post("/chaos/inject", json={"service_name": "catalog-svc", "fault_type": "ERROR_BURST", "magnitude": 1.0})
    drive(rt, clock, 8)
    assert db.query(Incident).filter_by(service_name="catalog-svc", status="OPEN").count() == 1
    # FAST_BURN's 5-minute window must drain before the service counts as quiet
    drive(
        rt, clock, 90, until=lambda: db.query(Incident).filter_by(service_name="catalog-svc", status="RESOLVED").count()
    )
    inc = db.query(Incident).filter_by(service_name="catalog-svc").one()
    assert inc.status == "RESOLVED" and inc.resolved_at is not None


def test_a_candidate_that_never_serves_names_the_gate_that_caught_it(client, rt, clock, db):
    """A canary whose smoke check fails is rolled back before any user traffic.
    The incident has to say so: this once read "no rollback condition met",
    because the engine re-derived the verdict from a threshold the smoke check
    does not use."""
    warm(rt, clock, 6)
    dep_id = _deploy(client, version="v3.0.0", config_diff={"FAULT_ERROR_RATE": 1.0})
    drive(rt, clock, 20, until=lambda: status(db, dep_id) == "ROLLED_BACK")

    assert status(db, dep_id) == "ROLLED_BACK"
    inc = db.query(Incident).filter_by(related_deployment_id=dep_id).one()
    assert "smoke check failed" in inc.trigger_reason
    assert inc.detection_context["trigger"] == "smoke_check_failed"
    assert inc.detection_context["release_specific"] is True
    assert not any(h["stage"].startswith("CANARY") for h in db.get(Deployment, dep_id).stage_history)
