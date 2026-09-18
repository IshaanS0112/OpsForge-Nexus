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
