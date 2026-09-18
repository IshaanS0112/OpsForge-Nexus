"""Alembic environment.

Two ways in:
- ``alembic upgrade head`` from the CLI builds an engine from DATABASE_URL.
- ``app.db.session.init_db`` passes an open connection through
  ``config.attributes["connection"]`` so startup migrations run on the exact
  engine the app uses.

``render_as_batch`` lets the same revisions run on SQLite, which cannot ALTER
columns in place.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import settings

config = context.config
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Revisions are written by hand against explicit column types, so autogenerate
# comparison is not wired up; the schema-drift test compares against the models.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
