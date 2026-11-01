"""Database test fixtures.

Each test session creates a throwaway database, migrates it, and drops it at the end.
Each test runs inside a transaction that is rolled back, so tests never see each other's
data. Requires PostgreSQL (`docker compose up -d db`) — skipped otherwise.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from auditor.db.migrate import migrate
from tests.db.factories import Seed

type DB = psycopg.Connection[dict[str, Any]]

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://auditor:auditor@localhost:5432/auditor"
)


@contextmanager
def fresh_database() -> Iterator[str]:
    """Create an empty database and yield its connection string; drop it afterwards."""
    name = f"auditor_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("PostgreSQL not reachable — run `docker compose up -d db`")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"'.encode())
        try:
            yield make_conninfo(ADMIN_URL, dbname=name)  # same server, different database
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'.encode())


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    with fresh_database() as url:
        migrate(url)
        yield url


@pytest.fixture
def db(migrated_url: str) -> Iterator[DB]:
    with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def seed(db: DB) -> Seed:
    return Seed(db)


def rows(conn: psycopg.Connection[Any], sql: str, **params: Any) -> list[dict[str, Any]]:
    """Run a query with %(name)s placeholders and return rows as dicts."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql.encode(), params or None)
        return cur.fetchall()
