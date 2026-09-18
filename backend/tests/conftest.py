"""Test fixtures: an in-memory SQLite DB, a control-loop runtime on the
simulated cluster, and a FastAPI TestClient.

The portable GUID/JSONType model definitions let the exact same ORM run on
SQLite here and PostgreSQL in prod. The runtime's control loop is not
started: tests call ``tick()`` themselves with a fake clock, so rollouts,
gates and drills are deterministic.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite://")
# Skip real inter-poll sleeps so the health gate runs instantly under test.
os.environ.setdefault("HEALTH_CHECK_FAST", "1")
os.environ.setdefault("CLUSTER_MODE", "simulated")
os.environ.setdefault("CONTROL_LOOP_ENABLED", "0")
os.environ.setdefault("LOADGEN_ENABLED", "0")
os.environ.setdefault("N8N_WEBHOOK_URL", "")

from app.db import session as db_session  # noqa: E402


class FakeClock:
    """One clock for the database timestamps (naive UTC datetimes) and the
    simulated pods (epoch seconds)."""

    def __init__(self, start: datetime = datetime(2026, 9, 18, 12, 0, 0)) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now.replace(tzinfo=UTC).timestamp()

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    # Point the app's Base/engine at the test engine, then create schema.
    db_session.engine = engine
    db_session.SessionLocal = TestingSessionLocal
    import app.models  # noqa: F401

    db_session.Base.metadata.create_all(bind=engine)

    s = TestingSessionLocal()
    try:
        yield s
    finally:
        s.close()
        db_session.Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Clear in-memory state on the shared engines between tests."""
    from app.services import anomaly_service, rollback_engine

    rollback_engine._cooldown_until.clear()
    anomaly_service.streak._counts.clear()
    yield
    rollback_engine._cooldown_until.clear()
    anomaly_service.streak._counts.clear()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def runtime(db, clock):
    """A control-loop runtime on the simulated cluster, with detector
    windows shortened so a baseline is learnt in a few simulated minutes."""
    from app import clock as clock_module
    from app.services import rollback_engine
    from app.services.anomaly_suite import AnomalySuite
    from app.services.incident_service import N8nNotifier
    from app.services.isolation_forest_detector import IsolationForestLayer
    from app.services.runtime import Runtime, set_runtime
    from app.services.seasonal_detector import SeasonalLayer
    from app.services.sim_cluster import SimulatedCluster

    clock_module.set_source(lambda: clock.now)
    rollback_engine._clock = clock
    cluster = SimulatedCluster(clock=clock, startup_delay_s=10.0, replicas=1)
    rt = Runtime(cluster, rollback=rollback_engine, loadgen=False, notifier=N8nNotifier(url=""))
    rt.suite = AnomalySuite(
        seasonal=SeasonalLayer(period_s=120, bucket_s=5, min_periods=2, refit_every_s=30),
        iforest=IsolationForestLayer(min_train=30, retrain_every=30, trees=50),
    )
    set_runtime(rt)
    rt.tick(clock.now)  # bootstrap: topology, seeds, routing
    yield rt
    set_runtime(None)
    import time

    clock_module.set_source(None)
    rollback_engine._clock = time.monotonic


@pytest.fixture()
def client(db, runtime):
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    def _override():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
