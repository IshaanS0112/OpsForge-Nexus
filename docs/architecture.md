# OpsForge Nexus — Architecture

OpsForge Nexus runs releases against a local Kubernetes cluster, watches six
services that call each other, decides when something is broken, works out
whether a failure is a root cause or a symptom of one, rolls back when the
evidence says so, and then tests all of that by breaking things on purpose.

This document covers the modules that carry that logic, how they fit
together, the data model, and the design decisions behind them. Every
threshold named here lives in [`backend/app/config.py`](../backend/app/config.py).

---

## Overview

```
 Ops Console (React) ──► FastAPI backend ─────────────────────────────► PostgreSQL
                           │
                           │  control loop, one tick every 5 s
                           │   ├─ gateway + load generator ──────► kind cluster
                           │   ├─ telemetry (pod CPU / memory)       6 services × blue/green
                           │   ├─ anomaly suite ─► consensus          Deployments, a live
                           │   ├─ burn-rate engine                    Service per service
                           │   ├─ deployment controller ─► canary / blue-green
                           │   ├─ rollback engine
                           │   ├─ incident service ─► cascade classification
                           │   └─ chaos engine
                           │
                           ├─ RCA engine: structured signals ─► Claude (schema-constrained) or rule-based fallback
                           ▼
                          n8n  incident opened ─► cascade tree ─► RCA ─► impact ─► notify (mock) ─► store
```

Everything left of the RCA engine is deterministic code. The model ranks and
explains causes over context the detectors have already computed; it never
decides whether something is anomalous, how fast the error budget is burning,
or whether to roll back.

---

## 1. The cluster and the workload

**Cluster.** `docker compose up` starts a `cluster-bootstrap` container that
creates a single-node [kind](https://kind.sigs.k8s.io/) cluster (Kubernetes
v1.37) through the mounted Docker socket, connects the node to the compose
network, and writes a kubeconfig into a shared volume. The step is idempotent:
a second `up` finds the cluster and reuses it. The backend talks to the API
server with the official `kubernetes` Python client
([`k8s_client.py`](../backend/app/services/k8s_client.py)).

**Workload.** Six services form a small shop:

```
notification-svc ─► checkout-api ─┬─► payments-svc ─► ledger-svc
                                  └─► inventory-svc ─► catalog-svc
```

Each one is the same small stdlib HTTP server
([`demo_service.py`](../backend/app/k8s/demo_service.py)) running on the stock
`python:3.12-alpine` image; the backend ships the file in a ConfigMap, so
there is no image to build or load into the cluster. `GET /work` does a little
CPU work behind a bounded worker pool (latency genuinely rises with load) and
then calls every dependency's `/work` over cluster DNS, so a slow or failing
dependency makes its callers slow or failing too: cascades travel through real
calls. Pods expose Prometheus-format CPU and memory on `/metrics`, fail
`/readyz` for 15 s after boot to model warm-up, and accept runtime faults on
`/admin/fault` with a TTL the pod enforces itself.

**Objects.** For each service the backend owns, through server-side apply:
two Deployments, `{svc}-blue` and `{svc}-green`; a live Service `{svc}` whose
selector picks a colour; and one pinned Service per colour, used for canary
traffic and health probes. All are NodePorts, so the backend reaches them from
the compose network. Every pod template carries a hash of the workload code. A
pod only picks up new code when it restarts, so at startup the backend rolls any
Deployment whose pods run older code than the ConfigMap now ships, the way
`kubectl rollout restart` would. A colour never mixes two versions of the code.

**Simulated mode.** [`sim_cluster.py`](../backend/app/services/sim_cluster.py)
implements the same interface in memory: pods with readiness delays, a queueing
latency model, fault injection, pod kills. The test suite and a Docker-free
local run use it. The API (`GET /cluster`) and the console always say which mode
is active.

---

## 2. The control loop

[`runtime.py`](../backend/app/services/runtime.py) runs one reconciliation tick
every 5 seconds:

1. drain the gateway's measurements of the last tick's edge traffic
2. scrape pod CPU and memory
3. run the anomaly suite per service, in dependency order
4. persist metrics (with every layer's output), SLI counts, error signatures
5. evaluate multiwindow burn rates; record alert transitions
6. open incidents where two detector layers agree or a FAST_BURN alert pages,
   and classify cascades
7. feed presumed-normal readings to the Isolation Forest
8. advance every in-flight rollout
9. check recently released versions for a post-deploy rollback
10. advance chaos drills
11. resolve incidents whose service has been quiet for 60 s
12. retention and downsampling

Rollouts are **controller-style**: the desired state lives in the `deployments`
table and each tick moves a rollout one step from wherever it is. A backend
restart resumes rollouts mid-flight rather than abandoning them.

---

## 3. Traffic and the SLI

[`gateway.py`](../backend/app/services/gateway.py) sends every edge request.
The **routing table** addresses the live Service normally; during a canary it
makes a weighted random choice between the stable colour's Service and the
canary colour's Service. The split therefore happens at the application
gateway, not in a service mesh.

The **load generator** is open-loop: Poisson arrivals at a rate that follows a
sinusoidal "day" (10 rps per service ± 50%), with a recurring burst at the same
phase of every day (2.5× for 5% of the cycle, a stand-in for a nightly batch
job). The next request is scheduled independently of the previous response, so
slow responses cannot throttle the offered load and hide latency (coordinated
omission). The demo compresses a day into 300 s (compose) or 600 s (default) so
the seasonal detector can learn a cycle in minutes.

The **SLI is request-based**: a request is bad if it returned a 5xx, failed at
the transport, or took longer than its service's latency threshold. Each tick
writes good and bad counts per service and per colour to `sli_samples`.

| Service | Calls | Latency threshold |
|---|---|---|
| notification-svc | checkout-api | 500 ms |
| checkout-api | payments-svc, inventory-svc | 450 ms |
| payments-svc | ledger-svc | 300 ms |
| inventory-svc | catalog-svc | 300 ms |
| ledger-svc | — | 250 ms |
| catalog-svc | — | 250 ms |

Every service's SLO is 99.9% of requests good over 30 days; targets and
thresholds are editable per service (`PUT /services/{name}/slo`).

---

## 4. Deployments: canary and blue-green

**State machine** ([`deployment_engine.py`](../backend/app/services/deployment_engine.py)),
with transitions allow-listed and illegal jumps raising:

```
IDLE → PREPARING_GREEN → DEPLOYING_GREEN → HEALTH_CHECKING ─┬─► CANARY_5 → CANARY_25 → CANARY_50 → CANARY_100 ─┐
                                                            └─► TRAFFIC_SWITCHING (blue-green) ◄───────────────┘
                                                                        │
                                                                        ▼
                                                                       LIVE
            any in-flight state ─► ROLLING_BACK ─► ROLLED_BACK
```

The state names keep V1's vocabulary: "green" means the candidate colour,
whichever colour that is for a given rollout.

**Per state** ([`deployment_controller.py`](../backend/app/services/deployment_controller.py)):

- `PREPARING_GREEN` applies the new version to the candidate colour's
  Deployment.
- `DEPLOYING_GREEN` reads the real rollout status, the same checks
  `kubectl rollout status` makes. It fails on the 180 s progress deadline, or
  early when a fatal pod state (`ImagePullBackOff`, `CrashLoopBackOff`, ...)
  holds for 20 s.
- `HEALTH_CHECKING` sends requests straight to the candidate. Blue-green runs
  V1's full health gate: one poll per tick over 60 s, where a poll passes on
  ≥ 99% success, p95 < 500 ms and no 5xx spike, and three consecutive failed
  polls roll back. Nothing else stands between a blue-green candidate and 100%
  of traffic. A canary only has to prove it serves at all (≥ 70% of 20 smoke
  requests); its stages judge its quality under real traffic.
- `CANARY_n` sends n% of edge traffic to the candidate and gates each stage
  (below).
- `TRAFFIC_SWITCHING` PATCHes the live Service's selector, so east-west traffic
  moves too. The old colour stays warm for an instant rollback.

**Canary gate.** A stage is judged on the canary colour's own SLI with Wald's
sequential probability ratio test
([`burn_rate_engine.py`](../backend/app/services/burn_rate_engine.py)):

```
H0: the canary burns at budget      p0 = 1 − SLO         (0.1% bad)
H1: the canary burns at page rate   p1 = 14.4 × (1 − SLO) (1.44% bad)
LLR = bad·ln(p1/p0) + good·ln((1−p1)/(1−p0))
FAIL as soon as LLR ≥ ln((1−β)/α) = ln 19 ≈ 2.94     (α = β = 0.05)
PASS when the 45 s window has elapsed with ≥ 20 canary requests and no FAIL
```

A canary is watched continuously, and a fixed-sample test that is re-checked
every tick fails healthy canaries far more often than its nominal rate. The
SPRT's boundary holds under continuous monitoring. One bad request adds 2.67,
so a single slow response never convicts a release, while two bad requests
close together do. The gate fails a canary as soon as the evidence is in and
passes it at the end of its window. Too little traffic for 300 s fails the stage:
an unmeasured canary is not a healthy one. With `auto_promote` off, a passing
stage waits for `POST /deployments/{id}/promote-canary`, which re-checks the
gate first.

What the gate can see depends on traffic. At 5% of about 10 rps a stage sees
roughly 20 canary requests, enough to convict a release that fails 15% of
requests within seconds but not one that fails 1.4%; the 25–100% stages see
hundreds. The gate compares against the SLO, not against the stable colour. A
breach during a rollout aborts the rollout whatever its cause, and the stable
colour's burn rate is recorded next to the verdict for context.

---

## 5. Rollback engine

[`rollback_engine.py`](../backend/app/services/rollback_engine.py). Every
trigger records the exact condition and value that fired, in the incident's
`trigger_reason` and the rollout's stage history.

| Trigger | Fires when | Applies to |
|---|---|---|
| `ROLLOUT_FAILED` | candidate never became Ready (deadline or a fatal pod state) | in-flight rollout |
| `CONSECUTIVE_HEALTH_FAILURES` | 3 failed health polls in a row | blue-green candidate |
| `CANARY_GATE_FAILED` | SPRT fail boundary crossed, or no traffic for 300 s | canary stage |
| `SLO_FAST_BURN` | FAST_BURN firing **and** ≥ 2 detector layers agree **and** the service is not a cascade victim | LIVE release, first 15 min |
| `MANUAL` | operator | latest rollout |

A rollback moves traffic first (the routing table, then the live selector if it
had moved), then scales the candidate to zero, and records the incident before
completing the Kubernetes calls, so a failed call is retried on the next tick
without losing the incident.

**Cascade victims are not rolled back.** When `checkout-api` burns because
`payments-svc` is failing, rolling back checkout's healthy release cannot fix
anything, and it destroys the warm standby that would be needed later.

**Anti-flapping.** After an `SLO_FAST_BURN` rollback, metric-triggered
rollbacks for that service are suppressed for 5 minutes: a rollback can itself
cause a transient anomaly, and acting on it would loop. Rollout-safety gates are
never suppressed; a candidate that fails its own gate must be aborted, not left
half rolled out.

**Only the latest rollout is watched.** A newer rollout reuses the standby
colour, so the release before it has no warm colour to flip back to. Manual
rollback refuses it for the same reason; the fix is to deploy the old version.

---

## 6. SLO burn-rate alerting

Method: Google SRE Workbook, chapter 5, "Alerting on SLOs": multiwindow,
multi-burn-rate alerts.

```
burn_rate = bad_ratio / (1 − SLO)
```

A burn rate of 1 spends exactly the error budget over the SLO period; 14.4
spends 2% of a 30-day budget in an hour. Each alert pairs a long window (is the
budget really being spent?) with a short one (is it still being spent?) and
fires only when both exceed the threshold, so one bad spike does not page and
the alert resets soon after recovery.

| Alert | Burn rate | Long / short window | Action |
|---|---|---|---|
| FAST_BURN | > 14.4 | 1 h / 5 m | page; opens an incident; rollback-eligible |
| MEDIUM_BURN | > 6 | 6 h / 30 m | ticket |
| SLOW_BURN | > 1 | 3 d / 6 h | weekly review |

The Workbook pages on the 6× tier too. Here it is a ticket, and only FAST_BURN
can open an incident or trigger a rollback. The short window needs at least 20
requests before an alert can fire. Windows are summed over the data that
exists, so a freshly started system reacts faster, not slower. SLI rows older
than two hours are compacted into 5-minute buckets, which keeps the 3-day and
30-day sums cheap; FAST_BURN always reads raw rows.

For a total outage the 1 h window crosses 14.4 after 3600 × 14.4 / 1000 ≈ 52 s,
the Workbook's detection time, and a test reproduces it. A partial failure takes
longer: a service failing 20% of requests pages after about 4 minutes. The
anomaly suite covers that gap.

---

## 7. Anomaly suite and consensus

Three independent, named methods judge the same four metrics per service every
tick: error rate, latency p95, CPU and memory
([`anomaly_suite.py`](../backend/app/services/anomaly_suite.py)).

| Layer | Method | Learns | Catches | Blind to |
|---|---|---|---|---|
| z-score | rolling window of 100 readings, z > 3 | recent level | sharp jumps on any one metric | recurring peaks (flags the batch job every day) |
| seasonal | STL decomposition (Cleveland et al., 1990); z-score of the residual | the shape of a day | jumps the daily cycle does not explain | anything until two full seasons of history exist |
| Isolation Forest | Liu, Ting & Zhou (2008), per service, 100 trees | the joint distribution of all four features | several metrics drifting together, none past its own 3σ | values beyond its training range grow no more anomalous |

Each layer flags a service after **K = 3 consecutive** anomalous readings.
**Two agreeing layers auto-act** (open an incident, make a LIVE release
rollback-eligible); one layer is shown on the console and does nothing.
Requiring independent methods to agree keeps false positives down without
raising any one method's threshold. A layer still warming up cannot vote, so
nothing auto-acts until two layers are warm, and the console shows each layer's
warm-up progress.

Refinements that came from running it:

- **Variance floor.** A healthy service's error rate is 0.0 every tick, so σ = 0
  and V1's z-score could never flag a burst on a service that had never errored.
  σ is floored per metric.
- **Clean baseline.** Readings flagged anomalous stay out of the window, so an
  ongoing incident does not inflate σ and talk the detector out of its own
  alarm. After a persistent level shift the window adapts: that is the new normal.
- **One-sided.** All four metrics are "higher is worse". A drop is often a
  symptom elsewhere: when payments fails, checkout fails fast and stops calling
  inventory, so catalog's CPU falls. That is real, but it is not catalog's problem.
- **Seasonal residual scale.** STL is fitted on 10 s bucket means, but live
  readings are single ticks, which are noisier. The residual scale is a robust
  (MAD) estimate from recent per-tick readings.
- **Isolation Forest inputs.** It trains only on presumed-normal ticks (no
  consensus, no open incident, no drill running) and does not score until it has
  60 of them. The threshold is the 0.5% quantile of its own training scores. The
  error ratio is add-one smoothed, (errors + 1) / (requests + 2). A service that
  never errored in training would otherwise give the forest a constant column it
  cannot split on, leaving it blind to an error burst.

**Scope: the model is unsupervised.** There are no labelled incidents to train
on, so the Isolation Forest makes no accuracy claim it could not back. It
answers "is this reading unlike the ones this service produced when nothing was
wrong?", nothing more. The tests check the behaviour each layer exists for: the
seasonal layer stays quiet through the daily batch burst that the z-score layer
flags every day, and the Isolation Forest flags a joint drift of all four
metrics that no single-metric z-score crosses.

---

## 8. Dependency graph and cascades

[`dependency_graph.py`](../backend/app/services/dependency_graph.py). The
graph is a DAG in `service_dependencies`: an edge (A, B) means A calls B. It is
seeded from the catalog and editable at runtime (`POST`/`DELETE
/services/graph/edges`); an edge that would create a cycle is rejected.

- **Blast radius** of a service: breadth-first over everything that depends on
  it, directly or transitively, in O(V + E), nearest first.
- **Topological order** (Kahn's algorithm) sets the order the control loop
  evaluates services in and the console's layout.
- **Cascade classification**
  ([`incident_service.py`](../backend/app/services/incident_service.py)):
  an incident is `is_cascading` when a service it depends on has an active
  incident that can explain it: detected at most 5 minutes before it, or at most
  90 s after it. It points at the root, the explaining upstream with no
  explaining upstream of its own, and chains collapse to the ultimate root. The
  window is re-classified whenever an incident opens, so a root detected after
  its dependents (a downstream service can trip a tick earlier) still collects
  them. One root failure produces one root incident with a cascade tree, not four
  unrelated-looking pages.

The bound is asymmetric because an effect cannot lead its cause by minutes. A
fault is detected within the 90 s detection bound of its onset, and a dependent
cannot degrade before its upstream does. A dependent that was already failing
well before the upstream incident stays an independent incident.

A rollback the evidence pins on the service's own release is never filed as a
cascade, whatever the timing. That covers a candidate that never served, and a
candidate that failed its gate while the stable colour, which calls the same
dependencies, stayed under the page burn rate. On the real cluster, a bad canary
on checkout-api was rolled back 15 s before a ledger drill began, which is
inside the 90 s allowance. Timing alone had filed that rollback under ledger's
cascade; the gate's evidence keeps it a root.

**Scope: the graph is hand-defined.** Edges are declared, not discovered. A
missing edge hides a cascade (the downstream incident looks independent); a
stale edge can blame the wrong root. Discovering edges from traces is out of
scope. What the graph does get from real traffic is corroboration. Each pod
reports the error it received from its dependency (`caused_by`), so checkout's
`UpstreamError` logs carry `InternalError:ledger-svc@v1.0.0` as their root
signature, and RCA sees both.

---

## 9. Chaos drills

[`chaos_engine.py`](../backend/app/services/chaos_engine.py). Break something
on purpose inside the cluster and measure the reliability machinery's response
instead of trusting it.

| Fault | Mechanism | Magnitude |
|---|---|---|
| `LATENCY_INJECTION` | each targeted pod adds latency to `/work` | milliseconds |
| `ERROR_BURST` | each targeted pod fails a share of `/work` with a 500 | share, 0–1 |
| `POD_KILL` | DELETE a share of the targeted pods; the ReplicaSet replaces them, and each replacement warms up for 15 s before it is Ready | share, 0–1 |

The target is all pods of a service, the stable colour, or the canary colour of
an in-flight canary. A canary-targeted drill stands in for a bad release, so a
rollback is expected. Faults travel through the API server's pod proxy and
expire inside the pod after their duration, so a drill cannot leave a fault
behind even if the backend dies.

The drill never tells the detectors anything. It measures on the control loop's
own signals:

- **detection latency**: injection to the first of the system's own mechanisms
  firing for that service (anomaly consensus, a FAST_BURN page, or for a
  canary-targeted drill the canary gate failing). Every mechanism's first firing
  goes on the timeline, not only the earliest.
- **rollback latency**: injection to the linked deployment reaching
  ROLLED_BACK.
- **recovery latency**: injection to the service being quiet again (no layer
  flagging, clean traffic) after the fault ends.
- **passed** = detected within 90 s, and for a deployment-linked drill rolled
  back within 90 s.

The fault runs for its full duration so every downstream mechanism gets
exercised: incidents, cascade classification, rollbacks. When a canary-targeted
drill's rollback completes, the rollback's incident is linked to the drill, so
the drill report and RCA see it. Drill verdicts are posted to the n8n webhook,
like incidents.

[`scripts/run_drills.py`](../scripts/run_drills.py) runs one drill per fault
type plus a canary-linked drill. It waits for quiet between drills, ranks each
root incident's causes told and blind, and records everything in
[`docs/drills/`](drills/).

---

## 10. RCA engine: structured signals before the model

[`rca_engine.py`](../backend/app/services/rca_engine.py). The order is
enforced in code:

1. **Lookback window**: 15 minutes before detection.
2. **Structured signals**: the recent deployment and its config diff; the
   rollout's stage history with each gate's verdict and counts; z-scored metric
   anomalies, each against its baseline from before the anomaly began; the top
   error signatures with counts, and the upstream root
   signatures they propagated from; a deploy-correlation flag; the dependency
   context (cascading or not, root service, blast radius); how many detector
   layers agreed; the burn rate; and any chaos drill that was running.
3. **Schema-constrained model call**: `claude-opus-5` through structured
   outputs (`messages.parse` with a Pydantic schema), so a reply is valid JSON of
   the right shape by construction. The prompt forbids speculation beyond the
   data, asks for the top three causes with confidence and cited evidence, and
   says a cascade's root outranks its symptoms. A declined request is re-run
   server-side on the recommended fallback model.
4. **Fallback**: if there is no key, the call fails, the model declines, or the
   answer is empty, a rule-based ranking over the same signals runs instead:
   injected fault, then cascade root, then the release and its rollout, then the
   service's strongest metric regression (the only evidence a latency fault
   leaves on a root service), then dominant error signatures. The report records
   which path produced it.

**Blind mode.** `POST /incidents/{id}/trigger-rca?blind=true` withholds the
chaos context, so a drill can test whether RCA finds the injected fault from the
evidence alone. The workload helps keep that honest: a bad release and an
injected error burst log the same signature (`InternalError:{svc}@{version}`),
as a real handler would, because it cannot tell why it failed.

---

## 11. Business impact

[`business_impact.py`](../backend/app/services/business_impact.py):

```
affected_requests  = requests in the incident window            (measured, sli_samples)
error_rate_delta   = incident error rate − baseline error rate   (clamped ≥ 0)
estimated_impact   = affected_requests × error_rate_delta × revenue_per_request
```

That is the requests that failed beyond what normally fails, priced per request.
The gateway counts every request, so `affected_requests` is real volume. V1 had
no request counter and used the ERROR-log count, which already counts failures,
so multiplying it by the error-rate delta applied the rate twice. The log proxy
remains only for services without measured traffic. For the root of a cascade
the sum runs over the root and every service that cascaded from it. Each
service's requests are distinct edge requests, so nothing is counted twice.
`revenue_per_request` is a configurable mock ($0.50). Every input is stored in
`calculation_basis`, so the number is auditable.

---

## 12. Incidents and n8n

- One active incident per service. A new trigger for a service that already has
  one escalates and annotates it instead of paging again.
- An incident resolves once its service has been quiet for 60 s (no detector
  layer flagging, no FAST_BURN, no rollout in flight). A service reported
  through external ingestion (`POST /metrics/ingest-batch`) counts as quiet from
  its first unflagged reading; a source that stops reporting never looks quiet.
- New incidents and finished drills are POSTed to `N8N_WEBHOOK_URL` when it is
  set, fire-and-forget, so notification never blocks the control loop.

The workflow ([`n8n/workflows/incident-rca-pipeline.json`](../n8n/workflows/incident-rca-pipeline.json))
routes root incidents through cascade tree → RCA → business impact → notify →
`POST /webhooks/n8n-callback`. Cascading incidents are pointed at their root
instead of being paged again, and drill verdicts go through the same notifier.
The notify nodes are NoOps until real credentials exist.

**Why n8n.** The response pipeline is a multi-step orchestration that ops teams
want to see and extend (add a PagerDuty or Jira node) without a code deploy.
Detection, gating and rollback stay in the backend; only the orchestration
lives in n8n.

---

## 13. Data model and migrations

`app/models/__init__.py`. The V1 tables (`deployments`, `incidents`, `metrics`,
`logs`, `rca_reports`, `business_impact`) gain columns for rollouts, cascades
and per-layer detector output. New tables: `service_dependencies`,
`anomaly_consensus`, `slo_config`, `sli_samples`, `burn_rate_alerts` and
`chaos_experiments`. Portable `GUID` and `JSONType` column types resolve to
native `UUID`/`JSONB` on PostgreSQL and `CHAR(36)`/`JSON` on SQLite, so the same
ORM serves production and the test suite.

Schema changes are Alembic migrations (`backend/migrations`): `0001` is the V1
baseline and `0002` adds V3. On startup, a V1 database without migration history
is stamped `0001` and upgraded in place; existing rows are kept. SQLite runs the
migrations in batch mode. [`schema.sql`](schema.sql) is the resulting PostgreSQL
DDL, for reading.

---

## 14. Ops Console

A React 19 + TypeScript dashboard (Vite, Tailwind 4), built for the screen that
is up during a 3 a.m. page: dark by default, with a light theme.

- **Console**: a status strip (health ring, open incidents, worst burn rate), a
  service rail, and the dependency graph as the centre of the page. Nodes pulse
  when anomalous, edges light up along an active cascade, and a blast-radius
  view dims everything a selected service cannot hurt. A drawer shows the
  selected incident (RCA, cascade tree, burn rate) or service (detector layers,
  metrics). A canary rail with each stage's gate and SPRT evidence appears only
  while a rollout is in flight.
- **Incident**: cascade tree, RCA candidates with cited evidence, burn-rate
  chart, business impact, detector layers at detection, timeline, error
  signatures.
- **Deployments**: start a canary or blue-green rollout, follow each stage's
  gate, roll back.
- **Chaos**: pick a fault, a target and a magnitude, watch the drill, read its
  report.

Charts are hand-built SVG: a symlog burn-rate chart with the 14.4×/6×/1×
thresholds, sparklines with a crosshair, and a health ring. Status colours are
reserved for status and always come with an icon and a label. Every chart has a
text alternative.

---

## Scope

- **A local, single-node cluster.** kind runs the real Kubernetes API, real
  Deployments, ReplicaSets, Services and pods, so rollouts, selector switches and
  pod kills are real. It is not a production cluster: one node, no autoscaling,
  no network policies, no ingress controller, and the workload is a demo service
  rather than a real shop.
- **The canary split is at the gateway, not a mesh.** Only edge (north-south)
  traffic is split. Pods call their dependencies through the live Service, so
  east-west traffic follows the selector, not the canary weight, until the final
  switch.
- **The anomaly model is unsupervised** (section 7).
- **The dependency graph is hand-defined** (section 8).
- **Traffic is synthetic.** The load generator is realistic in shape (open-loop,
  daily cycle, batch burst), but it is not user traffic.
- **Single tenant, no auth.** CORS is open for local use.
- **In-memory cooldown.** The rollback cooldown does not survive a restart;
  shared storage would fix that for multiple workers.
- **Notifications are mocked.** The n8n notify nodes are NoOps until
  credentials exist.

---

## Design decisions

1. **Controller-style reconciliation over background tasks.** V1 ran each
   rollout in a request's background task, which died with the process. Desired
   state in the database plus a reconcile loop resumes rollouts after a restart
   and puts every engine on one clock.
2. **Canary split at the gateway.** It needs no mesh and keeps the split
   observable in one place. The cost is that east-west traffic ignores the
   weight until the selector switch; that limit is stated rather than hidden.
3. **Burn rate as the primary rollback signal.** V1's "error rate > 5% for 30 s"
   treats a 5% blip on a service that normally fails 0% the same as on one that
   normally fails 4%. Burn rate measures failure against the service's own
   objective, and the two windows answer "is it real?" and "is it still
   happening?" separately. The rule comes from the SRE Workbook, not from
   invention.
4. **A sequential test for canary stages.** The gate is evaluated every tick.
   Re-checking a fixed-sample bound that often fails healthy canaries far more
   than its nominal rate; the first version of this gate used a Wilson lower
   bound and failed a canary on one bad request in two. The SPRT is built for
   repeated looks, and its boundary puts an explicit number (α = 5%) on the chance
   of failing a canary that only burns at budget.
5. **Consensus before auto-action.** Each detector has a known blind spot, and
   they are different ones. Two agreeing layers is a stronger signal than any one
   layer with a higher threshold, and a single layer still shows up on the
   console for a human.
6. **Cascade victims are not rolled back.** A rollback is only useful if the
   release caused the failure. Cascade classification feeds the rollback
   decision directly.
7. **Anti-flapping without blocking safety gates.** The cooldown after a
   post-deploy rollback breaks rollback → anomaly → rollback loops. Rollout
   gates are exempt; V1's cooldown also muted its health gate and left a failing
   deployment stuck in `HEALTH_CHECKING`.
8. **Structured RCA.** The value of a root-cause analysis is the correlation
   done before anyone writes a sentence. That work is deterministic code; the
   model ranks over it with a schema-constrained answer, and the fallback ranks
   the same evidence when the model is unavailable.
9. **Drills measure the system's claims.** Detection, rollback and recovery
   latency are measured on the system's own signals against stated bounds. Blind
   RCA tests the diagnosis the same way.
10. **Open-loop load.** A closed-loop generator slows down when the system
    slows down, which hides exactly the latency the detectors are meant to see.
11. **Scaling to many services.** Per-service detector state and cooldowns would
    move to shared storage, and the control loop would shard by service with
    ingestion behind a queue. Burn-rate windows already sum pre-aggregated SLI
    rows, and the graph algorithms are linear in the graph's size.
