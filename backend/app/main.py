"""OpsForge Nexus — FastAPI entrypoint."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import init_db
from app.routers import deployments, incidents, metrics, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="OpsForge Nexus",
    description="AI-powered release, reliability & incident intelligence platform.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # single-tenant MVP scope; restrict for production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(deployments.router)
app.include_router(incidents.router)
app.include_router(metrics.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "opsforge-nexus"}
