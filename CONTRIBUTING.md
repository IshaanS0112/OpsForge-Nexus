# Contributing

## Getting it running

```bash
docker compose up -d --build        # cluster, backend, console, n8n
```

Without Docker, the same backend runs against an in-memory cluster:

```bash
cd backend && pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///opsforge.db CLUSTER_MODE=simulated uvicorn app.main:app
```

## Before you open a pull request

```bash
cd backend && ruff check . && ruff format --check . && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

CI runs those plus a real-cluster smoke test on a fresh kind cluster.

## What a change needs

- **A test that fails without it.** For a bug fix, write the test first and
  watch it reproduce the bug; a fix with no failing test is a fix with no proof.
- **A reason, not a threshold.** Every automated decision in this system traces
  to a published method — see [`docs/methods.md`](docs/methods.md). A new
  threshold needs a source or a measurement behind it.
- **Honest nulls.** A figure the data cannot support is `None`, never `0`.
- **Measured claims only.** If a README or doc states a latency, a pass rate or
  a cost, it comes from a recorded run in [`docs/drills/`](docs/drills/) or from
  a test, never from an estimate.

## Where things live

| Path | What |
|---|---|
| `backend/app/services/` | The engines: control loop, detectors, gates, rollback, cascade, RCA, analytics |
| `backend/app/routers/` | HTTP surface, thin by design |
| `backend/app/k8s/` | Manifests and the demo workload that runs in the pods |
| `backend/migrations/` | Alembic revisions; schema changes ship as migrations |
| `frontend/src/` | Ops Console (React 19, Vite, Tailwind 4) |
| `scripts/` | Things you run against a live backend; standard library only |
| `docs/` | Architecture, methods, schema, recorded drills |

## Style

- Python: ruff, 120 columns, type hints on anything public. Docstrings explain
  *why*; inline comments are for the non-obvious only.
- TypeScript: no `any`, tokens from `index.css` for every colour, no raw hex.
- Commits: imperative subject describing the change in plain words, body
  explaining what the change is for.
