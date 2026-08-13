"""End-to-end API flow: healthy deploy, failing deploy -> incident -> RCA
(fallback, no API key in tests) -> business impact.

Deployment is async now (202 + background health gate), so we poll
GET /deployments/{id} for the terminal state."""


def _wait_terminal(client, dep_id: str, tries: int = 20) -> str:
    for _ in range(tries):
        status = client.get(f"/deployments/{dep_id}").json()["status"]
        if status in ("LIVE", "ROLLED_BACK"):
            return status
    return status


def test_healthy_deploy_goes_live(client):
    r = client.post("/deployments", json={"service_name": "web", "version": "v1.0.0"})
    assert r.status_code == 202
    assert r.json()["status"] == "IDLE"  # returns immediately, not terminal
    assert _wait_terminal(client, r.json()["id"]) == "LIVE"


def test_failing_deploy_rolls_back_and_opens_incident(client):
    r = client.post(
        "/deployments",
        json={"service_name": "checkout", "version": "v2.0.0", "simulate_failure": True},
    )
    assert r.status_code == 202
    assert _wait_terminal(client, r.json()["id"]) == "ROLLED_BACK"

    incidents = client.get("/incidents").json()
    assert len(incidents) == 1
    incident_id = incidents[0]["id"]
    assert "health check" in incidents[0]["trigger_reason"].lower()

    # RCA (falls back to rule-based since no API key in tests)
    rca = client.post(f"/incidents/{incident_id}/trigger-rca")
    assert rca.status_code == 200
    body = rca.json()
    assert len(body["root_cause_candidates"]) >= 1

    # Business impact is computed on demand and auditable
    impact = client.get(f"/incidents/{incident_id}/business-impact")
    assert impact.status_code == 200
    assert "formula" in impact.json()["calculation_basis"]


def test_metric_ingest_returns_zscore(client):
    # warm up a baseline with slight noise so variance > 0
    for i in range(20):
        v = 0.010 + (0.001 if i % 2 == 0 else -0.001)
        client.post("/metrics/ingest", json={"service_name": "api", "metric_name": "error_rate", "value": v})
    r = client.post("/metrics/ingest", json={"service_name": "api", "metric_name": "error_rate", "value": 0.5})
    assert r.status_code == 201
    assert r.json()["z_score"] is not None


def test_health_endpoint(client):
    assert client.get("/health").json()["status"] == "ok"
