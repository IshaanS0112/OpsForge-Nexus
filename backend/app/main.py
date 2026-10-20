"""OpsForge Nexus — FastAPI entrypoint."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import observability
from app.config import settings
from app.db.session import init_db
from app.routers import analytics, chaos, console, deployments, incidents, metrics, services, webhooks
from app.services.runtime import get_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
# The gateway and the metrics scrape make dozens of requests a second; one log
# line each would drown the control loop's own messages.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    runtime = get_runtime()
    if settings.control_loop_enabled:
        await runtime.start()
    try:
        yield
    finally:
        if settings.control_loop_enabled:
            await runtime.stop()


app = FastAPI(
    title="OpsForge Nexus",
    description=(
        "Release, reliability & incident intelligence: canary and blue-green rollouts on a local "
        "Kubernetes (kind) cluster, SLO burn-rate gating, a three-layer anomaly-detection suite with "
        "consensus, dependency-aware cascade detection, chaos drills, and structured RCA."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # single-tenant local scope; restrict for production
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (deployments, incidents, metrics, services, chaos, console, analytics, webhooks):
    app.include_router(r.router)

observability.register_runtime_collector(get_runtime)


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    # The route template, not the URL: /incidents/{incident_id} is one series,
    # while one series per incident id would be unbounded.
    route = request.scope.get("route")
    label = getattr(route, "path", "unmatched")
    observability.HTTP_REQUESTS.labels(method=request.method, route=label, status=response.status_code).inc()
    observability.HTTP_SECONDS.labels(method=request.method, route=label).observe(time.perf_counter() - started)
    return response


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "opsforge-nexus", "version": app.version}


@app.get("/metrics", tags=["meta"], response_class=Response)
def metrics_exposition() -> Response:
    """Prometheus exposition for the backend itself: control-loop timing, what
    it decided, and the API's own request counters."""
    return Response(observability.exposition(), media_type=observability.CONTENT_TYPE)
