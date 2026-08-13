"""RCA engine: structured signals precede the LLM, and a fallback exists."""
from datetime import datetime, timedelta

from app.models import Deployment, Incident, Log, Metric
from app.services.rca_engine import RCAEngine


def _seed_incident(db):
    now = datetime.utcnow()
    dep = Deployment(
        service_name="checkout",
        version="v1.4.2",
        status="ROLLED_BACK",
        config_diff={"flag": "new_pricing"},
        deployed_at=now - timedelta(minutes=4),
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)

    incident = Incident(
        service_name="checkout",
        status="OPEN",
        severity="HIGH",
        trigger_reason="3 consecutive failed health checks",
        related_deployment_id=dep.id,
        detected_at=now,
    )
    db.add(incident)
    db.add(Metric(service_name="checkout", metric_name="error_rate", value=0.12, z_score=4.2,
                  recorded_at=now - timedelta(seconds=30)))
    for _ in range(10):
        db.add(Log(service_name="checkout", level="ERROR",
                   message="NullPointerException:UserService.getProfile",
                   error_signature="NullPointerException:UserService.getProfile",
                   logged_at=now - timedelta(seconds=20)))
    db.commit()
    db.refresh(incident)
    return incident


def test_signals_collected_before_llm(db):
    incident = _seed_incident(db)
    engine = RCAEngine()
    signals = engine.collect_signals(db, incident)
    assert signals["recent_deployment"]["version"] == "v1.4.2"
    assert signals["correlation_flag"] == "deployment_within_5min_of_anomaly_onset"
    assert any(a["metric"] == "error_rate" for a in signals["metric_anomalies"])
    assert signals["top_error_signatures"][0]["signature"].startswith("NullPointer")


def test_fallback_when_llm_fails(db):
    incident = _seed_incident(db)

    class BoomClient:
        class messages:
            @staticmethod
            def create(**_):
                raise RuntimeError("llm down")

    engine = RCAEngine(llm_client=BoomClient())
    report = engine.run(db, incident)
    assert report.llm_model_used == "rule-based-fallback"
    assert len(report.root_cause_candidates) >= 1
    # deployment-proximity candidate ranked first
    assert "deployment" in report.root_cause_candidates[0]["cause"].lower()


def test_llm_success_path_parses_json(db):
    incident = _seed_incident(db)

    class OkClient:
        class _Msg:
            def __init__(self, text):
                self.content = [type("B", (), {"text": text})()]

        class messages:
            @staticmethod
            def create(**_):
                payload = (
                    '{"root_cause_candidates": [{"cause": "bad deploy", '
                    '"confidence": "high", "evidence": "342 NPEs"}]}'
                )
                return OkClient._Msg(payload)

    engine = RCAEngine(llm_client=OkClient())
    report = engine.run(db, incident)
    assert report.llm_model_used != "rule-based-fallback"
    assert report.root_cause_candidates[0]["cause"] == "bad deploy"


def test_parse_tolerates_code_fences():
    fenced = '```json\n{"root_cause_candidates": [{"cause": "x", "confidence": "low", "evidence": "y"}]}\n```'
    parsed = RCAEngine._parse_llm_json(fenced)
    assert parsed["root_cause_candidates"][0]["cause"] == "x"
