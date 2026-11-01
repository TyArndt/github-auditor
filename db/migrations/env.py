"""Alembic environment.

Order inside ONE transaction (all-or-nothing):
  1. drop the `reporting` schema (views)
  2. apply table migrations to the `inventory` schema
  3. if now at head, recreate views from db/views/*.sql
"""

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool, text

from auditor.config import get_db_settings
from auditor.db.models import SCHEMA, Base
from auditor.db.views import create_views, drop_views

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

VIEWS_DIR = Path(__file__).resolve().parent.parent / "views"
target_metadata = Base.metadata


def _database_url() -> str:
    # Tests pass the URL via attributes; otherwise use DATABASE_URL from env / .env.
    url = config.attributes.get("database_url")
    return str(url) if url else get_db_settings().database_url.get_secret_value()


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    # Autogenerate only compares the inventory schema (views are managed separately).
    if type_ == "schema":
        return name == SCHEMA
    return True


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=_include_name,
        compare_server_default=True,
        **kwargs,  # type: ignore[arg-type]
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout (`alembic upgrade head --sql`). Views are not included."""
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
            drop_views(connection)
            context.run_migrations()
            migration_ctx = context.get_context()
            if migration_ctx.get_current_revision() == context.get_head_revision():
                create_views(connection, VIEWS_DIR)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
