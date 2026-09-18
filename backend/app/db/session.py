"""SQLAlchemy engine + session factory, a FastAPI dependency, and startup
migrations."""

import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
V1_BASELINE_REVISION = "0001"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return cfg


def init_db() -> None:
    """Bring the schema to the latest migration.

    - Fresh database: run every revision.
    - Database created by V1's ``create_all`` (tables, no ``alembic_version``):
      stamp it at the V1 baseline, then apply the V3 delta on top.
    - Database created by ``create_all`` from the current models (the test
      suite does this): already at head, so stamp head.
    """
    from alembic import command

    import app.models  # noqa: F401  (register models on Base)

    tables = set(inspect(engine).get_table_names())
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if "deployments" in tables and "alembic_version" not in tables:
            if "service_dependencies" in tables:
                command.stamp(cfg, "head")
                return
            logger.info("V1 schema without migration history found; stamping %s", V1_BASELINE_REVISION)
            command.stamp(cfg, V1_BASELINE_REVISION)
        command.upgrade(cfg, "head")
