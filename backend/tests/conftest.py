"""Test fixtures: an in-memory SQLite DB and a FastAPI TestClient.

The portable GUID/JSONType model definitions let the exact same ORM run on
SQLite here and PostgreSQL in prod.
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite://")
# Skip real inter-poll sleeps so the health gate runs instantly under test.
os.environ.setdefault("HEALTH_CHECK_FAST", "1")

from app.db import session as db_session  # noqa: E402


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
def client(db):
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
