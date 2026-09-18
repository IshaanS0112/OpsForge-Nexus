"""OpsForge Nexus — FastAPI entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import init_db
from app.routers import chaos, console, deployments, incidents, metrics, services, webhooks
from app.services.runtime import get_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


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

for r in (deployments, incidents, metrics, services, chaos, console, webhooks):
    app.include_router(r.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "opsforge-nexus", "version": app.version}
