"""Alembic migrations: the revisions build exactly the schema the models
describe, and a V1 database upgrades in place without losing rows."""

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db import session as db_session


def _file_engine(tmp_path, name="db.sqlite"):
    return create_engine(f"sqlite:///{tmp_path / name}", future=True)


def _columns(engine) -> dict[str, set[str]]:
    insp = inspect(engine)
    return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}


def test_migrations_match_models(tmp_path, monkeypatch):
    engine = _file_engine(tmp_path)
    monkeypatch.setattr(db_session, "engine", engine)
    db_session.init_db()

    migrated = _columns(engine)
    expected = {table.name: {c.name for c in table.columns} for table in db_session.Base.metadata.sorted_tables}
    assert migrated == expected


def test_v1_database_upgrades_in_place(tmp_path, monkeypatch):
    engine = _file_engine(tmp_path)
    cfg = db_session._alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
    # V1 never had migration history: it created tables with create_all.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(
            text(
                "INSERT INTO deployments (id, service_name, version, status) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 'checkout', 'v1.0.0', 'LIVE')"
            )
        )

    monkeypatch.setattr(db_session, "engine", engine)
    db_session.init_db()

    cols = _columns(engine)
    assert {"strategy", "stage_history", "k8s_namespace"} <= cols["deployments"]
    assert {"is_cascading", "root_incident_id", "blast_radius_services"} <= cols["incidents"]
    assert "service_dependencies" in cols and "chaos_experiments" in cols
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version, status, strategy FROM deployments")).one()
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert tuple(row) == ("v1.0.0", "LIVE", "BLUE_GREEN")  # V1 rows were blue-green
    assert rev == "0002"


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    engine = _file_engine(tmp_path)
    monkeypatch.setattr(db_session, "engine", engine)
    db_session.init_db()
    db_session.init_db()  # second boot: nothing to do, must not raise
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"


def test_downgrade_returns_to_v1_shape(tmp_path):
    engine = _file_engine(tmp_path)
    cfg = db_session._alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0001")
    cols = _columns(engine)
    assert set(cols) == {"deployments", "incidents", "metrics", "logs", "rca_reports", "business_impact"}
    assert "strategy" not in cols["deployments"]
