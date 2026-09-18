"""RCA engine: structured signals precede the LLM, the model's output is
schema-constrained, and a fallback exists for every way the call can fail."""

from datetime import datetime, timedelta

from app.models import ChaosExperiment, Deployment, Incident, Log, Metric
from app.services.rca_engine import RCACandidate, RCAEngine, RCAResult


def _seed_incident(db, **incident_kw):
    now = datetime(2026, 9, 18, 12, 0, 0)
    dep = Deployment(
        service_name="checkout-api",
        version="v1.4.2",
        status="LIVE",
        config_diff={"flag": "new_pricing"},
        deployed_at=now - timedelta(minutes=4),
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)

    incident = Incident(
        service_name="checkout-api",
        status="OPEN",
        severity="HIGH",
        trigger_reason="2/3 anomaly layers agree (zscore, seasonal)",
        detected_at=now,
        detection_context={
            "consensus": {
                "zscore_flagged": True,
                "seasonal_flagged": True,
                "isolation_forest_flagged": False,
                "agreeing_layers": 2,
            },
            "burn": {
                "worst": "FAST_BURN",
                "slo_target": 0.999,
                "evaluations": [
                    {
                        "severity": "FAST_BURN",
                        "long_window": "1h",
                        "short_window": "5m",
                        "long_burn": 16.2,
                        "short_burn": 40.1,
                    },
                ],
            },
        },
        **incident_kw,
    )
    db.add(incident)
    db.add(
        Metric(
            service_name="checkout-api",
            metric_name="error_rate",
            value=0.12,
            z_score=4.2,
            recorded_at=now - timedelta(seconds=30),
        )
    )
    db.add(
        Log(
            service_name="checkout-api",
            level="ERROR",
            message="x",
            occurrences=10,
            error_signature="UpstreamError:checkout-api->payments-svc:500",
            root_signature="ChaosErrorBurst:ledger-svc",
            logged_at=now - timedelta(seconds=20),
        )
    )
    db.add(
        Log(
            service_name="checkout-api",
            level="ERROR",
            message="y",
            occurrences=3,
            error_signature="UpstreamTimeout:checkout-api->inventory-svc",
            logged_at=now - timedelta(seconds=20),
        )
    )
    db.commit()
    db.refresh(incident)
    return incident


class _Parsed:
    def __init__(self, parsed=None, stop_reason="end_turn", model="claude-opus-5"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = model


def _client(response=None, exc=None, calls=None):
    class _Messages:
        @staticmethod
        def parse(**kwargs):
            if calls is not None:
                calls.append(kwargs)
            if exc:
                raise exc
            return response

    class _Beta:
        messages = _Messages()

    class Client:
        beta = _Beta()

    return Client()


def test_signals_collected_before_llm(db):
    incident = _seed_incident(db)
    signals = RCAEngine().collect_signals(db, incident)
    assert signals["recent_deployment"]["version"] == "v1.4.2"
    assert signals["correlation_flag"] == "deployment_within_5min_of_anomaly_onset"
    assert any(a["metric"] == "error_rate" for a in signals["metric_anomalies"])
    # occurrences are summed, not rows counted
    assert signals["top_error_signatures"][0] == {
        "signature": "UpstreamError:checkout-api->payments-svc:500",
        "count": 10,
    }
    assert signals["upstream_root_signatures"][0]["signature"] == "ChaosErrorBurst:ledger-svc"
    assert signals["detector_consensus"]["agreeing_layers"] == 2
    assert signals["burn_rate"] == {
        "window": "1h",
        "value": 16.2,
        "short_window": "5m",
        "short_value": 40.1,
        "severity": "FAST_BURN",
        "slo_target": 0.999,
    }
    assert signals["dependency_context"]["is_cascading"] is False


def test_cascade_and_chaos_context_reach_the_signals(db):
    root = Incident(
        service_name="ledger-svc", status="OPEN", trigger_reason="x", detected_at=datetime(2026, 9, 18, 11, 59)
    )
    exp = ChaosExperiment(
        service_name="ledger-svc",
        fault_type="ERROR_BURST",
        magnitude=1.0,
        target="all",
        injected_at=datetime(2026, 9, 18, 11, 58),
    )
    db.add_all([root, exp])
    db.commit()
    root.chaos_experiment_id = exp.id
    db.commit()
    incident = _seed_incident(
        db, is_cascading=True, root_incident_id=root.id, blast_radius_services=["notification-svc"]
    )

    engine = RCAEngine(llm_client=_client(exc=RuntimeError("llm down")))
    signals = engine.collect_signals(db, incident)
    assert signals["dependency_context"]["root_service"] == "ledger-svc"
    assert signals["chaos_context"]["fault_type"] == "ERROR_BURST"  # inherited from the root incident

    report = engine.run(db, incident)
    causes = [c["cause"] for c in report.root_cause_candidates]
    assert causes[0].startswith("Injected fault: chaos drill ERROR_BURST on ledger-svc")
    assert "Upstream failure in ledger-svc" in causes[1]


def test_fallback_when_llm_fails(db):
    incident = _seed_incident(db)
    report = RCAEngine(llm_client=_client(exc=RuntimeError("llm down"))).run(db, incident)
    assert report.llm_model_used == "rule-based-fallback"
    assert len(report.root_cause_candidates) >= 1
    # deployment-proximity candidate ranked first
    assert "deployment" in report.root_cause_candidates[0]["cause"].lower()
    assert report.signals["recent_deployment"]["version"] == "v1.4.2"  # the context is stored either way


def test_llm_success_path_uses_structured_output(db):
    incident = _seed_incident(db)
    calls: list[dict] = []
    parsed = RCAResult(root_cause_candidates=[RCACandidate(cause="bad deploy", confidence="high", evidence="342 NPEs")])
    engine = RCAEngine(llm_client=_client(_Parsed(parsed), calls=calls))
    report = engine.run(db, incident)
    assert report.llm_model_used == "claude-opus-5"
    assert report.root_cause_candidates[0]["cause"] == "bad deploy"
    sent = calls[0]
    assert sent["output_format"] is RCAResult  # schema-constrained, no free-text JSON parsing
    assert sent["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["betas"]
    assert '"burn_rate"' in sent["messages"][0]["content"]  # structured context, not a log dump


def test_refusal_falls_back_to_rules(db):
    incident = _seed_incident(db)
    report = RCAEngine(llm_client=_client(_Parsed(None, stop_reason="refusal"))).run(db, incident)
    assert report.llm_model_used == "rule-based-fallback"


def test_empty_output_falls_back_to_rules(db):
    incident = _seed_incident(db)
    report = RCAEngine(llm_client=_client(_Parsed(None, stop_reason="max_tokens"))).run(db, incident)
    assert report.llm_model_used == "rule-based-fallback"
