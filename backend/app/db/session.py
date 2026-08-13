"""SQLAlchemy engine + session factory and a FastAPI dependency."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Used for the SQLite-based test suite and first boot."""
    import app.models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)
