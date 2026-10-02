# OpsForge Nexus

**Release, reliability and incident intelligence on a real Kubernetes cluster: canary
rollouts gated by SLO burn rate, a three-layer anomaly suite that has to agree before it
acts, dependency-aware cascade detection, chaos drills that measure all of it, and a
root-cause pipeline that collects structured signals before it ever calls a model.**

FastAPI · React 19 + TypeScript · PostgreSQL · kind (Kubernetes 1.37) · n8n · Claude · 136 backend + 8 frontend tests

[![CI](https://github.com/IshaanS0112/OpsForge-Nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/IshaanS0112/OpsForge-Nexus/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org)

---

## Why I built this

Most "AI incident tooling" I read about works one way: dump the logs into an LLM
and print whatever comes back. That bothered me, because the useful part of a
root-cause analysis isn't the prose. It's the correlation done *before* anyone
writes a sentence: which deploy landed just before the error rate moved, which
error signature dominates, whether the metrics are genuinely anomalous or just
noisy.

So I built the version where that correlation is real code and the model only
enters at the end. V1 did it for one service on simulated infrastructure. V3
runs the same idea against a real Kubernetes cluster. It follows a failure
across services instead of treating each one alone, replaces fixed thresholds
with the SRE Workbook's burn-rate alerts, and then checks its own claims by
breaking things on purpose. What is real and what is simulated is spelled out
[below](#what-is-real-vs-simulated).

---

## What it does

- **Rollouts on a real cluster.** Each of six services runs as blue and green
  Deployments in a local kind cluster. A rollout applies the new version to the
  standby colour, watches the real rollout status, then either walks a canary
  through 5 → 25 → 50 → 100% of traffic or runs a blue-green health gate. It
  finishes by PATCHing the live Service's selector. Unpullable images and crash
  loops fail fast.
- **Canary stages gated by statistics, not a threshold.** Each stage is judged
  on the canary's own SLI with a sequential probability ratio test: 5% chance of
  failing a canary that only burns at budget, and two bad requests close together
  are enough to convict a release that fails 15% of its traffic.
- **SLO burn-rate alerting.** Multiwindow, multi-burn-rate alerts (14.4× over
  1 h/5 m, 6× over 6 h/30 m, 1× over 3 d/6 h), straight from Google's SRE
  Workbook. A FAST_BURN page is the primary post-deploy rollback trigger.
- **Three detectors that must agree.** Rolling z-score, STL seasonal residual and
  an unsupervised Isolation Forest judge every service every 5 seconds. Two
  agreeing layers act; one layer only shows up on the console.
- **Cascades, not N pages.** A hand-defined dependency graph gives blast radius
  and root classification. One failing service opens one root incident with its
  dependents filed under it, and a cascade victim's release is never rolled back.
- **Chaos drills.** Latency, error bursts and pod kills are injected through the
  Kubernetes API, and the system measures its own detection, rollback and
  recovery latency against stated bounds.
- **Structured RCA.** Deploy history, gate verdicts, z-scored anomalies, error
  signatures with the upstream signature they came from, cascade context and burn
  rate are collected first. Then `claude-opus-5` ranks causes with
  schema-constrained output, or a rule-based fallback ranks the same evidence.
  A blind mode hides the drill, to test whether RCA finds the fault on evidence
  alone.
- **Ops Console.** A dark-first React dashboard centred on a live dependency
  graph: pulsing nodes, lit cascade edges, a blast-radius view, a canary rail
  with each stage's SPRT evidence, and incident, deployment and chaos screens.

---

## Verified on a real cluster

Recorded on the local kind cluster (Kubernetes v1.37, one node). Every record,
with its timeline, incidents and both RCA rankings, is in
[`docs/drills/`](docs/drills/); `python3 scripts/run_drills.py` produces a new set.

| Fault | Service | Verdict | Detected | By | Rolled back | Recovered | Cascade |
|---|---|---|---|---|---|---|---|
| Latency +400 ms | ledger-svc | pass | 5.5 s | burn-rate page (consensus at 15.7 s) | — | 66.6 s | payments-svc, checkout-api, notification-svc |
| Error burst, half of requests | inventory-svc | pass | 13.2 s | detector consensus | — | 69.2 s | checkout-api, notification-svc |
| Pod kill, every pod | payments-svc | pass | 11.5 s | burn-rate page (consensus at 16.5 s) | — | 62.5 s | checkout-api, notification-svc |
| Error burst on the canary colour | catalog-svc | pass | 13.7 s | canary burn-rate gate | 13.7 s | 64.9 s | — |

Three things worth reading off that table:

- **The error burst never paged.** Sixty seconds of half-failing requests is about
  1% of an hour's error budget, so the 1 h burn rate reached 9.96, under the 14.4
  page threshold. Detector consensus caught it in 13.2 s. That gap is the reason
  the anomaly suite exists next to the burn-rate alerts, and the reason two of
  them have to agree before anything automatic happens.
- **The first pod-kill drill failed**, and the record
  ([`…-pod-kill.json`](docs/drills/20260919T214511-pod-kill.json)) is kept. Killing
  the only pod caused no measurable outage at all: those pods still carried a
  3-second readiness delay from an earlier session instead of the configured 15,
  so the replacement was Ready before anything could notice. The backend now
  reconciles Deployments that have drifted from its own settings at startup, and
  the re-run passed.
- **Rollouts are checked the same way.** `python3 scripts/kind_smoke.py` — what CI
  runs on a fresh cluster — ships an unpullable image (rolled back in 21 s, traffic
  never left the stable colour), a blue-green release (selector switched), and a
  canary that walked 5 → 25 → 50 → 100% with all four burn-rate gates passing.

---

## Architecture

```
 Ops Console (React) ──► FastAPI backend ──────────────────────────► PostgreSQL
                           │ control loop, every 5 s
                           │  gateway + open-loop load ──────────► kind cluster
                           │  anomaly suite ─► consensus            6 services × blue/green
                           │  burn-rate engine                      live Service per service
                           │  deployment controller ─► canary / blue-green
                           │  rollback engine · incidents ─► cascade classification
                           │  chaos engine
                           ├─ RCA: structured signals ─► Claude (or rule-based fallback)
                           ▼
                          n8n: incident ─► cascade tree ─► RCA ─► impact ─► notify ─► store
```

Everything left of the RCA engine is deterministic code; the model never decides
anomalies, burn rates or rollbacks. Full detail, including the design decisions
behind each module: [`docs/architecture.md`](docs/architecture.md).

---

## Quickstart

### Everything, on a local kind cluster

Needs Docker with about 3 GB of memory free (the kind node uses about 1.5 GB).

```bash
docker compose up -d --build
```

The first run creates the kind cluster, which takes a minute or two. After
that, `up` reuses the cluster and restarts its node if it was stopped.

For RCA to call the model, give compose a key: either
`ANTHROPIC_API_KEY=sk-ant-... docker compose up -d`, or a line
`ANTHROPIC_API_KEY=...` in a `.env` file next to `docker-compose.yml`
(git-ignored). Without a key, RCA uses its rule-based fallback, and every report
says which path produced it.

| Service | URL |
|---|---|
| Ops Console | http://localhost:8080 |
| API docs | http://localhost:8000/docs |
| Backend metrics (Prometheus) | http://localhost:8000/metrics |
| n8n | http://localhost:5678 |

The cluster is yours to inspect. `cluster-up.sh` writes a host kubeconfig to
`.kube/config`, which is git-ignored:

```bash
kubectl --kubeconfig .kube/config -n opsforge get deploy,svc,pods
```

The anomaly layers learn a baseline from live traffic first. The z-score and
Isolation Forest are warm after about five minutes; the seasonal layer needs two
compressed "days" (10 minutes). The console shows each layer's warm-up. Until two
layers are warm, burn-rate alerts still page but consensus cannot act.

To stop, run `docker compose down`; the cluster keeps running. To remove the
cluster too, run `docker rm -f opsforge-control-plane`, or
`kind delete cluster --name opsforge` if you have kind installed.

### Without Docker (simulated cluster)

The same backend runs against an in-memory cluster with the same interface.
The console labels which mode is active.

```bash
cd backend
pip install -r requirements.txt
DATABASE_URL=sqlite:///opsforge.db CLUSTER_MODE=simulated uvicorn app.main:app
```

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8000
```

Run this way, the backend also reads `backend/.env` (see `backend/.env.example`)
for settings such as `ANTHROPIC_API_KEY`.

---

## Try it

The scripts use only the Python standard library.

```bash
./scripts/demo.sh                        # healthy canary, bad canary, latency drill, RCA, impact
python3 scripts/run_drills.py            # the drill suite; records land in docs/drills/
python3 scripts/metric_generator.py --inject   # bring-your-own telemetry through the same suite
python3 scripts/capture_rca_sample.py    # with a key: shows RCA running on the model, not the fallback
```

Everything the scripts do is also a button in the console: start a rollout on
the Deployments screen, pick a fault on the Chaos screen, run RCA from an
incident.

**n8n.** Import [`n8n/workflows/incident-rca-pipeline.json`](n8n/workflows/incident-rca-pipeline.json)
and activate it. Then start the backend with
`N8N_WEBHOOK_URL=http://n8n:5678/webhook/opsforge-incident` (in the environment
or the root `.env`, like the API key), and every new root
incident runs cascade tree → RCA → business impact → notify → store. Cascading
incidents are pointed at their root instead of paged, and drill verdicts go
through the same notifier. The notify nodes are NoOps until you add real
credentials.

---

## Upgrading from V1

The backend migrates a V1 database in place on startup. It finds the V1 tables
without migration history, stamps them as the Alembic baseline (`0001`), and
upgrades to V3 (`0002`). Deployments, incidents, RCA reports and impact rows are
kept.

Raw metrics and logs follow V3's retention (`DATA_RETENTION_HOURS`, 96 h), so
V1 telemetry older than that is deleted by the first retention pass. To keep
it, back it up before the first V3 start:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_dump -U opsforge opsforge > opsforge-v1.sql
```

---

## Tests

```bash
cd backend && pip install -r requirements-dev.txt
ruff check . && pytest          # 136 tests

cd frontend && npm ci
npm run typecheck && npm test   # 8 tests
```

The backend tests drive the control loop on the simulated cluster with a fake
clock. Covered: rollouts end to end (healthy canary, gate-failed canary,
blue-green, unpullable image, a canary-targeted drill, post-deploy rollback on
FAST_BURN plus consensus, a cascade victim left alone), the SPRT gate's
false-fail rate under continuous monitoring, the Workbook's 52-second FAST_BURN
detection, each detector's reason to exist (the seasonal layer ignoring a daily
batch burst that the z-score flags, the Isolation Forest catching a joint drift
no single metric shows), cascade classification, RCA's signal collection,
schema-constrained call and every fallback path, and the V1 → V3 migration. The
demo workload's own HTTP behaviour is tested against a real subprocess.

CI runs lint, the backend tests, the frontend typecheck, tests and build.

---

## What is real vs simulated

| Real | Simulated or mocked |
|---|---|
| Kubernetes: Deployments, ReplicaSets, Services and pods in a kind cluster, driven through the API server | The cluster is one local node, not a production cluster |
| Rollouts: server-side apply, rollout status like `kubectl rollout status`, selector switch, scale to zero | The workload is a small demo service, not a real shop |
| Traffic: real HTTP to every pod, with real latency, timeouts and 5xx | The traffic is synthetic (open-loop Poisson with a daily cycle and a batch burst) |
| Cascades: pods call their dependencies over cluster DNS, so failures propagate through real calls | The dependency graph is declared, not discovered from traces |
| Faults: injected into running pods through the pod proxy; pod kills are real deletes | Notifications are NoOp nodes until credentials exist |
| Detection, burn rates, gates, cascades, rollbacks: deterministic code on measured data | `revenue_per_request` is a configurable mock |

The canary split happens at the application gateway, not in a service mesh. Only
edge traffic is split; east-west calls follow the live selector until the final
switch. The anomaly model is unsupervised, so it makes no accuracy claim it
could not back. Both limits, and the others, are in
[Scope](docs/architecture.md#scope).

---

## Design decisions

The rationale for each module is in [`docs/architecture.md`](docs/architecture.md#design-decisions):
controller-style reconciliation, the gateway-level split, burn rate as the
rollback signal, a sequential test for canary stages, consensus before
auto-action, never rolling back a cascade victim, anti-flapping that does not
mute safety gates, structured RCA, drills that measure the system's claims,
open-loop load, and scaling to many services.

---

## Tech stack

FastAPI · SQLAlchemy + Alembic · PostgreSQL · kind + the Kubernetes Python client ·
NumPy, statsmodels (STL), scikit-learn (Isolation Forest) · React 19, TypeScript,
Vite, Tailwind 4 · n8n · Claude API · Docker Compose.

## License

MIT
