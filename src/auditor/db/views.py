"""Manage the `reporting` schema: SQL views that Metabase and the API read.

Views are not versioned like migrations. Every `migrate()` drops the whole `reporting`
schema before applying table migrations and recreates it from `db/views/*.sql` afterwards
(in filename order). Table changes therefore never collide with view dependencies, and a
view edit only needs the .sql file changed plus `auditor db migrate`.
"""

from pathlib import Path

import psycopg

REPORTING_SCHEMA = "reporting"
READER_ROLE = "auditor_read"


def drop_views(conn: psycopg.Connection) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {REPORTING_SCHEMA} CASCADE")


def create_views(conn: psycopg.Connection, views_dir: Path) -> list[str]:
    """Create the reporting schema and every view in `views_dir`. Returns files applied."""
    conn.execute(f"CREATE SCHEMA {REPORTING_SCHEMA}")
    applied = []
    for sql_file in sorted(views_dir.glob("*.sql")):
        # bytes => sent verbatim, so '%' in LIKE patterns isn't treated as a placeholder.
        conn.execute(sql_file.read_bytes())
        applied.append(sql_file.name)
    _grant_reader(conn)
    return applied


def _grant_reader(conn: psycopg.Connection) -> None:
    """The read-only role sees views only; the `inventory` tables stay off-limits."""
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READER_ROLE,)).fetchone()
    if row is None:
        return
    conn.execute(f"GRANT USAGE ON SCHEMA {REPORTING_SCHEMA} TO {READER_ROLE}")
    conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {REPORTING_SCHEMA} TO {READER_ROLE}")
