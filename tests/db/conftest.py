"""Database test fixtures.

Each test session creates a throwaway database, migrates it to head, and drops it at the
end. Each test runs inside a transaction that is rolled back, so tests never see each
other's data. Requires PostgreSQL (`docker compose up -d db`) — skipped otherwise.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from tests.db.factories import Seed

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://auditor:auditor@localhost:5432/auditor"
)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@contextmanager
def fresh_database() -> Iterator[str]:
    """Create an empty database and yield its URL; drop it afterwards."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"auditor_test_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        pytest.skip("PostgreSQL not reachable — run `docker compose up -d db`")
    try:
        yield make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    with fresh_database() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
def db(migrated_url: str) -> Iterator[Connection]:
    engine = create_engine(migrated_url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
    engine.dispose()


@pytest.fixture
def seed(db: Connection) -> Seed:
    return Seed(db)


def rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]
