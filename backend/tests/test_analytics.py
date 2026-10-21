"""Reliability analytics: the figures an operations review reads, derived from
the stored rows, and honest about a window that cannot answer."""

from datetime import datetime, timedelta

from app.clock import utcnow
from app.models import BusinessImpact, ChaosExperiment, Deployment, Incident, SLISample, SLOConfig
from app.services import analytics

NOW = datetime(2026, 10, 20, 12, 0, 0)


def test_percentile_uses_nearest_rank():
    assert analytics.percentile([], 95) is None
    assert analytics.percentile([10.0], 50) == 10.0
    assert analytics.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert analytics.percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0


def test_ratio_reports_nothing_rather_than_zero_for_an_empty_sample():
    assert analytics.ratio(1, 4) == 0.25
    assert analytics.ratio(0, 0) is None


def test_detection_source_reads_the_reason_the_opener_recorded():
    cases = {
        "Rollback of v2: canary gate failed: burning at 500x": "canary gate",
        "Rollback of v2: rollout failed: ImagePullBackOff for 20s": "rollout gate",
        "Rollback of v2: 3 consecutive failed health checks": "health gate",
        "Rollback of v2: smoke check failed: the candidate is not serving": "smoke check",
        "2/3 anomaly layers agree (zscore, seasonal)": "detector consensus",
        "FAST_BURN 1h/5m at 19.8x/69.5x": "burn-rate page",
        "something new": "other",
    }
    for reason, expected in cases.items():
        assert analytics.detection_source(reason) == expected


def _seed(db, now=NOW):
    # merge, not add: the runtime fixture may already have seeded this service
    db.merge(SLOConfig(service_name="payments-svc", slo_target=0.999, window_days=30, latency_threshold_ms=300.0))
    db.add_all(
        [
            Deployment(
                service_name="checkout-api",
                version="v1.1.0",
                status="LIVE",
                strategy="CANARY",
                deployed_at=now - timedelta(hours=5),
                finished_at=now - timedelta(hours=5) + timedelta(seconds=200),
            ),
            Deployment(
                service_name="payments-svc",
                version="v2.0.0",
                status="ROLLED_BACK",
                strategy="CANARY",
                deployed_at=now - timedelta(hours=4),
                finished_at=now - timedelta(hours=4) + timedelta(seconds=100),
            ),
            # Outside the window: must not count anywhere.
            Deployment(
                service_name="ledger-svc",
                version="v0.9.0",
                status="LIVE",
                strategy="BLUE_GREEN",
                deployed_at=now - timedelta(days=30),
                finished_at=now - timedelta(days=30) + timedelta(seconds=50),
            ),
            SLISample(
                service_name="payments-svc",
                window_start=now - timedelta(hours=4),
                window_end=now - timedelta(hours=3),
                total_requests=1000,
                bad_requests=50,
                error_requests=50,
            ),
            SLISample(
                service_name="checkout-api",
                window_start=now - timedelta(hours=4),
                window_end=now - timedelta(hours=3),
                total_requests=1000,
                bad_requests=0,
                error_requests=0,
            ),
            ChaosExperiment(
                service_name="payments-svc",
                fault_type="ERROR_BURST",
                injected_at=now - timedelta(hours=4),
                status="COMPLETED",
                passed=True,
                detection_latency_seconds=12.0,
                recovery_latency_seconds=60.0,
                target="all",
            ),
        ]
    )
    root = Incident(
        service_name="payments-svc",
        detected_at=now - timedelta(hours=4),
        resolved_at=now - timedelta(hours=4) + timedelta(seconds=300),
        status="RESOLVED",
        severity="CRITICAL",
        trigger_reason="FAST_BURN 1h/5m at 19.8x/69.5x",
        is_cascading=False,
    )
    child = Incident(
        service_name="checkout-api",
        detected_at=now - timedelta(hours=4),
        status="OPEN",
        severity="HIGH",
        trigger_reason="2/3 anomaly layers agree (zscore, seasonal)",
        is_cascading=True,
    )
    db.add_all([root, child])
    db.commit()
    db.add(BusinessImpact(incident_id=root.id, affected_requests=1000, estimated_impact_value=250.0, calculated_at=now))
    db.add(BusinessImpact(incident_id=child.id, affected_requests=400, estimated_impact_value=100.0, calculated_at=now))
    db.commit()


def test_summary_answers_the_four_questions_from_stored_rows(db):
    _seed(db)
    out = analytics.summary(db, NOW, days=1)

    delivery = out["delivery"]
    assert delivery["releases"] == 1 and delivery["rollbacks"] == 1
    assert delivery["change_failure_rate"] == 0.5  # the 30-day-old release is outside the window
    assert delivery["duration_p50_s"] == 100.0 and delivery["duration_p95_s"] == 200.0
    assert delivery["by_strategy"] == {"CANARY": {"released": 1, "rolled_back": 1}}

    rel = out["reliability"]
    payments = next(s for s in rel["services"] if s["service"] == "payments-svc")
    assert payments["availability"] == 0.95 and payments["meets_slo"] is False
    assert rel["services_meeting_slo"] == 1 and rel["worst_service"] == "payments-svc"
    assert rel["fleet_availability"] == 0.975

    response = out["response"]
    assert response["incidents"] == 2 and response["cascading_incidents"] == 1
    assert response["mttr_s"] == 300.0
    assert response["detection_sources"] == {"burn-rate page": 1, "detector consensus": 1}
    assert response["drills"] == 1 and response["drills_passed"] == 1 and response["mttd_s"] == 12.0

    cost = out["cost"]
    assert cost["total_value"] == 350.0 and cost["priced_incidents"] == 2
    assert cost["worst_service"] == "payments-svc"
    assert cost["by_service"][0] == {"service": "payments-svc", "value": 250.0, "incidents": 1}


def test_an_empty_window_reports_nothing_measured_rather_than_zero(db):
    _seed(db)
    out = analytics.summary(db, NOW - timedelta(days=10), days=1)
    assert out["delivery"]["releases"] == 0
    assert out["delivery"]["change_failure_rate"] is None
    assert out["reliability"]["fleet_availability"] is None
    assert out["response"]["mttr_s"] is None and out["response"]["mttd_s"] is None
    assert out["cost"]["total_value"] == 0.0


def test_summary_endpoint_serves_the_same_figures(client, db):
    _seed(db, now=utcnow())  # the API reads the app clock, which tests drive
    body = client.get("/analytics/summary", params={"days": 90}).json()
    assert body["window"]["days"] == 90
    assert body["delivery"]["releases"] >= 1
    assert client.get("/analytics/summary", params={"days": 0}).status_code == 422


def test_recalculated_impact_counts_the_incident_once(db):
    """Impact rows are append-only, so asking for a report twice must not
    double the cost: the newest figure per incident wins."""
    _seed(db)
    incident = next(i for i in db.query(Incident).all() if i.service_name == "payments-svc")
    db.add(
        BusinessImpact(
            incident_id=incident.id,
            affected_requests=1200,
            estimated_impact_value=300.0,
            calculated_at=NOW + timedelta(minutes=1),
        )
    )
    db.commit()

    cost = analytics.summary(db, NOW, days=1)["cost"]
    assert cost["priced_incidents"] == 2  # two incidents, three rows
    assert cost["total_value"] == 400.0  # 300 (recalculated) + 100, not 650
    assert cost["by_service"][0] == {"service": "payments-svc", "value": 300.0, "incidents": 1}
