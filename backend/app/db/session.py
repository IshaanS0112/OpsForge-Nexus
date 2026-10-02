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
# A database without migration history sits at the newest revision whose
# marker table exists. One entry per revision that creates a table, newest first.
SCHEMA_MARKERS = (
    ("0003", "rollback_cooldowns"),
    ("0002", "service_dependencies"),
    ("0001", "deployments"),
)


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
    - Database created by ``create_all`` (V1 did this, and so does the test
      suite): no ``alembic_version`` table, so work out which revision its
      tables correspond to, stamp that, and apply everything newer.
    """
    from alembic import command

    import app.models  # noqa: F401  (register models on Base)

    tables = set(inspect(engine).get_table_names())
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if "deployments" in tables and "alembic_version" not in tables:
            revision = next(rev for rev, marker in SCHEMA_MARKERS if marker in tables)
            logger.info("schema without migration history found; stamping %s", revision)
            command.stamp(cfg, revision)
        command.upgrade(cfg, "head")
