"""Dependency DAG, blast radius, root inference, and cascade classification."""

from datetime import datetime, timedelta

import pytest

from app import catalog
from app.models import Incident
from app.services.dependency_graph import CycleError, DependencyGraph
from app.services.incident_service import IncidentService, N8nNotifier

G = DependencyGraph(catalog.edges())


def test_upstream_and_downstream():
    assert G.upstream("checkout-api") == ["inventory-svc", "payments-svc"]
    assert G.downstream("checkout-api") == ["notification-svc"]
    assert G.upstream_closure("notification-svc") == [
        "checkout-api",
        "inventory-svc",
        "payments-svc",
        "catalog-svc",
        "ledger-svc",
    ]


def test_blast_radius_is_bfs_over_dependents_nearest_first():
    assert G.blast_radius("ledger-svc") == ["payments-svc", "checkout-api", "notification-svc"]
    assert G.blast_radius("notification-svc") == []
    assert "inventory-svc" not in G.blast_radius("payments-svc")


def test_topological_order_puts_dependencies_first():
    order = G.topological_order()
    for service, dep in catalog.edges():
        assert order.index(dep) < order.index(service)


def test_layers_for_layout():
    layers = G.layers()
    assert layers["ledger-svc"] == 0 and layers["payments-svc"] == 1
    assert layers["checkout-api"] == 2 and layers["notification-svc"] == 3


def test_cycles_are_rejected():
    with pytest.raises(CycleError, match="cycle"):
        G.with_edge("ledger-svc", "notification-svc")
    with pytest.raises(CycleError):
        DependencyGraph([("a", "a")])


def test_roots_among_anomalous_services():
    assert G.roots_among({"ledger-svc", "payments-svc", "checkout-api"}) == ["ledger-svc"]
    # two independent failures: both are roots
    assert G.roots_among({"ledger-svc", "catalog-svc", "checkout-api"}) == ["catalog-svc", "ledger-svc"]


def _open(db, svc, now):
    inc, _ = IncidentService(N8nNotifier(url="")).open(db, svc, "test", severity="HIGH", now=now, graph=G)
    return inc


def test_downstream_detected_first_is_reparented_when_root_appears(db):
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    checkout = _open(db, "checkout-api", t0)
    assert not checkout.is_cascading  # nothing upstream is anomalous yet
    payments = _open(db, "payments-svc", t0 + timedelta(seconds=5))
    db.refresh(checkout)
    assert checkout.is_cascading and checkout.root_incident_id == payments.id
    ledger = _open(db, "ledger-svc", t0 + timedelta(seconds=10))  # the real root trips last
    db.refresh(checkout)
    db.refresh(payments)
    assert not ledger.is_cascading
    assert payments.root_incident_id == ledger.id and checkout.root_incident_id == ledger.id


def test_incidents_outside_the_window_are_independent(db):
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    ledger = _open(db, "ledger-svc", t0)
    payments = _open(db, "payments-svc", t0 + timedelta(minutes=6))  # W = 5 min
    assert not payments.is_cascading
    assert ledger.blast_radius_services == ["payments-svc", "checkout-api", "notification-svc"]


def test_dependent_failing_well_before_the_upstream_stays_independent(db):
    # checkout-api had its own bad rollout; a ledger fault four minutes later
    # cannot have caused it, but it does explain payments-svc.
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    checkout = _open(db, "checkout-api", t0)
    ledger = _open(db, "ledger-svc", t0 + timedelta(minutes=4))
    payments = _open(db, "payments-svc", t0 + timedelta(minutes=4, seconds=10))
    db.refresh(checkout)
    assert not checkout.is_cascading and checkout.root_incident_id is None
    assert payments.is_cascading and payments.root_incident_id == ledger.id


def test_release_specific_incident_is_never_a_cascade(db):
    # checkout-api's canary failed its gate while its stable colour was fine;
    # a ledger fault 15 s later (inside the skew allowance) must not claim it.
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    service = IncidentService(N8nNotifier(url=""))
    checkout, _ = service.open(
        db, "checkout-api", "Rollback of v1.2", severity="HIGH", now=t0, graph=G, release_specific=True
    )
    ledger = _open(db, "ledger-svc", t0 + timedelta(seconds=15))
    notification = _open(db, "notification-svc", t0 + timedelta(seconds=16))
    db.refresh(checkout)
    assert not checkout.is_cascading and checkout.detection_context["release_specific"] is True
    assert notification.root_incident_id == ledger.id


def test_cascade_spanning_more_than_the_window_keeps_one_root(db):
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    ledger = _open(db, "ledger-svc", t0)
    payments = _open(db, "payments-svc", t0 + timedelta(minutes=4, seconds=30))
    checkout = _open(db, "checkout-api", t0 + timedelta(minutes=5, seconds=30))  # 5.5 min after ledger
    db.refresh(payments)
    assert payments.root_incident_id == ledger.id  # an old root still counts
    assert checkout.is_cascading and checkout.root_incident_id == ledger.id  # via payments


def test_one_active_incident_per_service(db):
    service = IncidentService(N8nNotifier(url=""))
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    first, created = service.open(db, "ledger-svc", "consensus", severity="MEDIUM", now=t0, graph=G)
    again, created_again = service.open(db, "ledger-svc", "FAST_BURN", severity="CRITICAL", now=t0, graph=G)
    assert created and not created_again and again.id == first.id
    assert again.severity == "CRITICAL"  # escalated, not duplicated
    assert db.query(Incident).count() == 1
    assert service.notifier.sent[0]["event"] == "incident_opened"  # one page, not two
    assert len(service.notifier.sent) == 1
