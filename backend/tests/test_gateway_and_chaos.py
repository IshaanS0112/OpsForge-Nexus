"""Gateway routing and measurement, load shape, drill verdicts, and
cascade-wide business impact."""

import random
from datetime import datetime, timedelta

import pytest

from app.models import Incident, SLISample
from app.services.business_impact import BusinessImpactService
from app.services.chaos_engine import evaluate_drill
from app.services.gateway import LoadGenerator, Outcome, RoutingTable, TrafficRecorder, percentile


def test_routing_splits_edge_traffic_by_weight():
    rt = RoutingTable(rng=random.Random(0))
    rt.set_live("svc", "blue")
    assert {rt.choose("svc")[0] for _ in range(50)} == {"live"}
    rt.set_canary("svc", "blue", "green", 25)
    picks = [rt.choose("svc")[0] for _ in range(4000)]
    assert 0.22 < picks.count("green") / len(picks) < 0.28
    rt.set_canary("svc", "blue", "green", 100)
    assert {rt.choose("svc")[0] for _ in range(50)} == {"green"}


def test_recorder_counts_bad_requests_against_the_slo():
    rec = TrafficRecorder()
    rec.set_latency_bound("svc", 300)
    rec.record(Outcome("svc", "live", "blue", 200, 50))
    rec.record(Outcome("svc", "live", "blue", 200, 450))  # slow: bad, not an error
    rec.record(Outcome("svc", "green", "green", 500, 20, "InternalError:svc@v2"))
    rec.record(Outcome("svc", "live", "blue", 502, 30, "UpstreamError:svc->dep:500", "InternalError:dep@v1"))
    rec.record(Outcome("svc", "live", "blue", None, 3000, "GatewayTimeout:svc"))
    w = rec.drain()["svc"]
    assert (w.total, w.bad, w.errors) == (5, 4, 3)
    assert w.colors["green"].total == 1 and w.colors["blue"].bad == 3
    assert w.signatures()[("UpstreamError:svc->dep:500", "InternalError:dep@v1")] == 1
    assert rec.drain() == {}  # drained


def test_percentile_is_nearest_rank():
    values = sorted(float(i) for i in range(1, 101))
    assert percentile(values, 0.95) == 95.0
    assert percentile([7.0], 0.95) == 7.0


def test_load_follows_the_daily_curve_and_batch_burst():
    lg = LoadGenerator(
        gateway=None,
        services=[],
        rps=10,
        period_s=600,
        amplitude=0.5,
        burst_start=0.2,
        burst_share=0.05,
        burst_multiplier=2.5,
    )
    assert lg.rate(0) == pytest.approx(10)
    assert lg.rate(150) == pytest.approx(15)  # quarter cycle: the peak
    assert lg.rate(450) == pytest.approx(5)
    in_burst = 0.22 * 600
    assert lg.rate(in_burst) > 2.4 * 10 * (1 + 0.5 * 0.9)  # 2.5x on top of the curve


@pytest.mark.parametrize(
    "detect, rollback, linked, passed",
    [
        (15.0, None, False, True),
        (None, None, False, False),
        (95.0, None, False, False),
        (12.0, 30.0, True, True),
        (12.0, None, True, False),
        (12.0, 120.0, True, False),
    ],
)
def test_drill_verdict(detect, rollback, linked, passed):
    ok, verdict = evaluate_drill(detect, rollback, linked, 90.0, 90.0)
    assert ok is passed
    assert verdict.startswith("PASS" if passed else "FAIL")


def test_business_impact_sums_the_cascade(db):
    t0 = datetime(2026, 9, 18, 12, 0, 0)
    root = Incident(
        service_name="ledger-svc",
        status="RESOLVED",
        trigger_reason="x",
        detected_at=t0,
        resolved_at=t0 + timedelta(minutes=3),
    )
    db.add(root)
    db.commit()
    child = Incident(
        service_name="payments-svc",
        status="RESOLVED",
        trigger_reason="x",
        detected_at=t0,
        resolved_at=t0 + timedelta(minutes=3),
        is_cascading=True,
        root_incident_id=root.id,
    )
    db.add(child)
    for svc in ("ledger-svc", "payments-svc"):
        # baseline: clean traffic before the incident window
        db.add(
            SLISample(
                service_name=svc,
                color="blue",
                window_start=t0 - timedelta(minutes=10),
                window_end=t0 - timedelta(minutes=5),
                total_requests=1000,
                bad_requests=0,
            )
        )
        # incident: 1000 requests, 400 bad
        db.add(
            SLISample(
                service_name=svc,
                color="blue",
                window_start=t0,
                window_end=t0 + timedelta(minutes=1),
                total_requests=1000,
                bad_requests=400,
            )
        )
    db.commit()

    impact = BusinessImpactService().calculate(db, root)
    basis = impact.calculation_basis
    assert basis["scope"].startswith("cascade")
    assert [p["service"] for p in basis["per_service"]] == ["ledger-svc", "payments-svc"]
    assert impact.affected_requests == 2000
    # each service: 1000 requests * 0.4 excess bad ratio * $0.50
    assert impact.estimated_impact_value == pytest.approx(400.0)
    assert basis["affected_requests_source"] == "measured"

    child_impact = BusinessImpactService().calculate(db, child)
    assert child_impact.affected_requests == 1000
    assert "cascades from its root" in child_impact.calculation_basis["note"]
