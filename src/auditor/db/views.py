"""Manage the `reporting` schema: SQL views that Metabase and the API read.

Views are not versioned migration-by-migration. Instead, every `alembic upgrade` drops the
whole `reporting` schema before migrating tables and recreates it from `db/views/*.sql`
afterwards (in filename order). Table changes therefore never collide with view
dependencies, and a view edit only needs the .sql file changed plus an upgrade.
"""

from pathlib import Path

from sqlalchemy import Connection, text

REPORTING_SCHEMA = "reporting"
READER_ROLE = "auditor_read"


def drop_views(conn: Connection) -> None:
    conn.execute(text(f"DROP SCHEMA IF EXISTS {REPORTING_SCHEMA} CASCADE"))


def create_views(conn: Connection, views_dir: Path) -> list[str]:
    """Create the reporting schema and every view in `views_dir`. Returns files applied."""
    conn.execute(text(f"CREATE SCHEMA {REPORTING_SCHEMA}"))
    applied = []
    dbapi_conn = conn.connection.dbapi_connection
    assert dbapi_conn is not None
    for sql_file in sorted(views_dir.glob("*.sql")):
        # Raw DBAPI cursor with no parameters: SQL is sent verbatim (no bind-param parsing).
        cur = dbapi_conn.cursor()
        try:
            cur.execute(sql_file.read_text(encoding="utf-8"))
        finally:
            cur.close()
        applied.append(sql_file.name)
    _grant_reader(conn)
    return applied


def _grant_reader(conn: Connection) -> None:
    """The read-only role sees views only; the `inventory` tables stay off-limits."""
    exists = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER_ROLE}
    ).scalar()
    if not exists:
        return
    conn.execute(text(f"GRANT USAGE ON SCHEMA {REPORTING_SCHEMA} TO {READER_ROLE}"))
    conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {REPORTING_SCHEMA} TO {READER_ROLE}"))
