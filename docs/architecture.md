# OpsForge Nexus — Architecture

This document explains the five modules that carry the real logic, the data
model, the request flows, and the key design decisions behind them.

---

## 1. Blue-green deployment engine

**State machine** (`app/services/deployment_engine.py`):

```
IDLE → PREPARING_GREEN → DEPLOYING_GREEN → HEALTH_CHECKING
     → TRAFFIC_SWITCHING → LIVE
                │
                ▼ (health gate fails)
          ROLLING_BACK → ROLLED_BACK
```

Transitions are enforced by an explicit allow-list (`_TRANSITIONS`); any illegal
jump raises `IllegalTransition`. The machine (`DeploymentStateMachine`) is pure
and DB-free, so it is unit-tested in isolation. Orchestration and persistence
live in `app/services/orchestrator.py`, keeping side effects out of the machine.

**Health-check gate** (`HealthChecker`): polls a sampler every `interval_s` over
a `window_s` window, **sleeping `interval_s` between polls so the gate is
genuinely time-based**, not a single instant check. A poll passes only if:

- HTTP 200 success rate ≥ 99%, **and**
- p95 latency < 500 ms, **and**
- no 5xx spike.

Three consecutive failed polls fail the gate and hand off to the rollback
engine. In production the sampler hits the green environment's health endpoint;
in V1 it is a simulated sampler (`healthy_sampler` / `failing_sampler`). The
`HEALTH_CHECK_FAST` flag skips the inter-poll sleeps for tests and demos; it is
`False` by default so production polling is real.

**Asynchronous execution:** `POST /deployments` returns `202` immediately with
the record at `IDLE`; the full state-machine run + health gate execute in a
FastAPI background task (`orchestrator.execute_deployment`) with its own DB
session, so the request never blocks on the polling window. Clients poll
`GET /deployments/{id}` for the terminal state. Traffic switch is an instant
flag flip in V1 (canary % is V2).

---

## 2. Rollback engine

`app/services/rollback_engine.py`. Concrete, logged trigger conditions:

| Condition | Threshold | Window |
|---|---|---|
| Consecutive health-check failures | ≥ 3 | — |
| Error rate | > 5% | sustained 30s |
| p95 latency | > 2× baseline | sustained 60s |
| Manual trigger | user-initiated | — |

Every firing records the exact metric/value in the incident's `trigger_reason`,
so triage is auditable rather than a guess.

**Anti-flapping cooldown:** after a rollback, automatic triggers for that
service are suppressed for 5 minutes. This breaks the rollback → transient
anomaly → rollback loop. Manual rollback bypasses the cooldown (operator
override). The cooldown is in-memory for V1; production would back it with Redis
so it survives restarts and is shared across workers.

---

## 3. Anomaly detector (z-score)

`app/services/anomaly_detector.py`. For each metric, maintain a rolling window of
the last N=100 readings, compute `z = (value − μ) / σ`, and flag `|z| > 3.0`. An
incident is signalled only after K=3 **consecutive** anomalous readings, which
suppresses one-off spikes.

`zscore()` returns `None` while the baseline is still warming up (fewer than 2
points, or zero variance) — treated as "not anomalous yet", never a false
positive.

**Why z-score over a fixed threshold:** it adapts to each service's own baseline,
so no per-service manual tuning. **Tradeoff:** needs enough history for a stable
baseline and is weaker under strong seasonality (daily traffic cycles). This is a
documented limitation, not hidden — V2 replaces it with a trained model.

---

## 4. RCA engine — structured signals *before* the LLM

`app/services/rca_engine.py`. This is the module that decides whether the whole
project reads as real engineering. The order is enforced in code:

1. **Lookback window:** `detected_at − 15min → detected_at`.
2. **Collect structured signals** (`collect_signals`): recent deployment (version,
   config diff, minutes-before-incident), z-scored metric anomalies with
   baselines, top-5 deduplicated error signatures with counts, and a correlation
   flag when a deploy landed within 5 minutes of anomaly onset.
3. **Constrained LLM call** (`_call_llm`): a JSON-only system prompt that forbids
   speculation beyond the provided data and asks for the top-3 ranked causes with
   confidence + cited evidence.
4. **Parse & validate** (`_parse_llm_json`): tolerates code fences, requires the
   `root_cause_candidates` shape, raises otherwise.

**Fallback** (`_rule_based_fallback`): if the LLM errors/times out, rank
candidates by deployment proximity and dominant error signatures, so the
pipeline always returns something defensible. The stored report records
`llm_model_used = "rule-based-fallback"` for transparency.

Example collected signal payload:

```json
{
  "recent_deployment": {"version": "v1.4.2", "minutes_before_incident": 4, "config_diff": {}},
  "metric_anomalies": [
    {"metric": "error_rate", "z_score": 4.2, "value": 0.12, "baseline": 0.01}
  ],
  "top_error_signatures": [
    {"signature": "NullPointerException:UserService.getProfile", "count": 342}
  ],
  "correlation_flag": "deployment_within_5min_of_anomaly_onset"
}
```

---

## 5. Business impact calculator

`app/services/business_impact.py`:

```
affected_requests = COUNT(ERROR logs in incident window)
error_rate_delta  = incident_error_rate − baseline_error_rate   (clamped ≥ 0)
estimated_impact  = affected_requests × error_rate_delta × revenue_per_request
```

**Window semantics:** the signals that *trigger* an incident precede its
detection, so the incident window is `[detected_at − 15min, detected_at]` and the
baseline is the 15 minutes before that. (An earlier version measured the incident
window *after* `detected_at` and mis-read the anomaly as baseline — fixed.)
`affected_requests` is proxied by ERROR-log volume in the incident window (each
error ≈ one impacted request) — more defensible than counting metric samples; in
production this comes from a real request/error counter.

`revenue_per_request` is a configurable business parameter (mock default $0.50).
Every input is stored in `calculation_basis` (JSONB) so the number is auditable —
"here is the formula and the inputs", not a black box. In production
`revenue_per_request` comes from finance/analytics' real conversion data.

---

## Data model

Six tables (`app/models/__init__.py`): `deployments`, `incidents`, `metrics`,
`logs`, `rca_reports`, `business_impact`. Portable `GUID` and `JSONType` column
types resolve to native `UUID`/`JSONB` on PostgreSQL and `CHAR(36)`/`JSON` on
SQLite, so the same ORM powers prod and the test suite with no schema drift.
`metrics(service_name, recorded_at)` is indexed for the rolling-window queries.

Canonical PostgreSQL DDL for reference: [`schema.sql`](schema.sql).

---

## Request flows

**Deployment (async):** `POST /deployments` persists the record at `IDLE` and
returns `202`; a background task drives the state machine through the real
time-based health gate → on pass, `TRAFFIC_SWITCHING → LIVE`; on fail, the
rollback engine evaluates → `ROLLING_BACK → ROLLED_BACK`, opens an incident,
seeds correlated failure signals, starts the cooldown. Clients poll
`GET /deployments/{id}` for the terminal state.

**Metric ingestion:** `POST /metrics/ingest` → z-score computed against the
rolling window → K-consecutive anomalies auto-open an incident.

**Incident response (n8n):** webhook → `GET /incidents/{id}` →
`POST /incidents/{id}/trigger-rca` and `GET /incidents/{id}/business-impact` →
combine → notify → `POST /webhooks/n8n-callback` stores the combined report. The
workflow JSON is exported at
[`n8n/workflows/incident-rca-pipeline.json`](../n8n/workflows/incident-rca-pipeline.json).

**Why n8n and not just backend function calls:** the incident-response pipeline
is a multi-step, human-visible orchestration (fetch → RCA → impact → notify →
store) that ops teams want to *see*, edit, and extend (add a PagerDuty node, a
Jira node) without a code deploy. That is exactly n8n's remit. The core
detection/rollback logic stays in the backend where it belongs; only the
orchestration lives in n8n.

---

## Non-goals (V1 scope)

- No real Kubernetes orchestration — deployment states are simulated.
- No multi-cloud, no real load-balancer integration (traffic switch is a flag).
- No canary % rollout in V1 (instant switch; canary is V2).
- No custom-trained ML anomaly model — z-score is enough and more defensible.
- No multi-tenant auth — single implicit tenant.
- No real Slack/PagerDuty unless credentials exist — the notify node is a NoOp
  mock by default, stated honestly.

**V2 roadmap:** real Kubernetes API integration; canary rollout
(5→25→50→100%); multi-service dependency graph for cascading-incident
detection; ML-based anomaly detection; Redis-backed cooldown/streak state.

---

## Design decisions

1. **Blue-green state machine.** IDLE → PREPARING_GREEN → DEPLOYING_GREEN →
   HEALTH_CHECKING → TRAFFIC_SWITCHING → LIVE, with a rollback branch out of the
   middle states; transitions are allow-listed and illegal jumps raise.
2. **Rollback triggers and false-positive avoidance.** Four concrete conditions
   (table above). False positives are avoided by *sustained-window* thresholds
   (30s / 60s) and the 3-strike health rule, not single-sample reactions.
3. **Z-score over a fixed threshold.** Chosen for per-service adaptivity; the
   tradeoff is needing baseline history and weakness under seasonality.
4. **Structured RCA, not log-prompting.** Structured signal collection (deploy +
   z-scored anomalies + counted error signatures + correlation flag) happens
   before any LLM call.
5. **Candidate ranking and confidence.** The LLM ranks top-3 and must cite
   specific evidence (error counts, z-scores, deploy proximity); the fallback
   ranks by deployment proximity and error dominance.
6. **LLM failure handling.** The rule-based fallback returns ranked candidates;
   the report records that the fallback was used.
7. **Business-impact inputs.** `revenue_per_request` is a configurable
   parameter; the framework and audit trail are the point — real conversion data
   replaces the constant in production.
8. **n8n over in-backend functions.** Human-visible, editable multi-step
   orchestration; core detection/rollback logic stays in the backend.
9. **Preventing rollback → anomaly → rollback loops.** A post-rollback cooldown
   suppresses automatic triggers for 5 minutes.
10. **Scaling to 100 services.** Per-service rolling windows and cooldown state
    move to Redis; metric ingestion moves behind a queue; the detector is
    stateless per service; the dependency graph (V2) handles cascading incidents.
```
