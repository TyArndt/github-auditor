"""Minimal forward-only migration runner (plain SQL files, psycopg only).

    db/migrations/NNNN_description.sql   applied in order, each exactly once
    inventory.schema_migrations          records version, filename, checksum, applied_at

One `migrate()` call runs in ONE transaction (all-or-nothing):
  1. take an advisory lock so two runners can't migrate at once
  2. drop the `reporting` schema (views)
  3. apply pending migration files, recording each
  4. recreate every view from db/views/*.sql and re-grant the read-only role

Applied migrations must never be edited: their checksum is verified on every run.
There are no down-migrations; fix forward with a new file (or restore from backup).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from auditor.db.views import create_views, drop_views

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
VIEWS_DIR = REPO_ROOT / "db" / "views"

SCHEMA = "inventory"
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x6175646974  # arbitrary constant ("audit") for pg_advisory_xact_lock


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        # Normalize line endings so a Windows checkout doesn't look like an edit.
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode()).hexdigest()


@dataclass(frozen=True)
class Status:
    applied: list[str]
    pending: list[str]


def to_psycopg_url(url: str) -> str:
    """Accept SQLAlchemy-style `postgresql+psycopg://` URLs too."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"Bad migration filename (want NNNN_description.sql): {path.name}")
        migrations.append(Migration(match.group(1), path))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"Duplicate migration version in {migrations_dir}")
    return migrations


def _ensure_history_table(conn: psycopg.Connection) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
            version    text        PRIMARY KEY,
            filename   text        NOT NULL,
            checksum   text        NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(f"SELECT version, checksum FROM {SCHEMA}.schema_migrations").fetchall()
    return {version: checksum for version, checksum in rows}


def _verify(applied: dict[str, str], migrations: list[Migration]) -> list[Migration]:
    """Check history against files; return the pending migrations."""
    by_version = {m.version: m for m in migrations}
    unknown = sorted(set(applied) - set(by_version))
    if unknown:
        raise MigrationError(f"Database has migrations with no file: {unknown}")
    for version, checksum in applied.items():
        if by_version[version].checksum != checksum:
            raise MigrationError(
                f"Migration {by_version[version].path.name} was edited after it was applied. "
                "Revert the edit and add a new migration instead."
            )
    pending = [m for m in migrations if m.version not in applied]
    if applied and pending and pending[0].version < max(applied):
        raise MigrationError(
            f"Migration {pending[0].path.name} is older than already-applied ones; renumber it."
        )
    return pending


def migrate(
    url: str, migrations_dir: Path = MIGRATIONS_DIR, views_dir: Path = VIEWS_DIR
) -> list[str]:
    """Apply pending migrations and rebuild views. Returns filenames applied."""
    migrations = discover(migrations_dir)
    with psycopg.connect(to_psycopg_url(url)) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _ensure_history_table(conn)
        pending = _verify(_applied(conn), migrations)
        drop_views(conn)
        for migration in pending:
            conn.execute(migration.sql.encode())  # bytes => sent verbatim, no %-placeholders
            conn.execute(
                f"INSERT INTO {SCHEMA}.schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                (migration.version, migration.path.name, migration.checksum),
            )
        create_views(conn, views_dir)
    return [m.path.name for m in pending]


def status(url: str, migrations_dir: Path = MIGRATIONS_DIR) -> Status:
    migrations = discover(migrations_dir)
    with psycopg.connect(to_psycopg_url(url)) as conn:
        _ensure_history_table(conn)
        applied = _applied(conn)
        conn.rollback()  # status is read-only; don't persist the CREATE IF NOT EXISTS
    pending = _verify(applied, migrations)
    return Status(
        applied=[m.path.name for m in migrations if m.version in applied],
        pending=[m.path.name for m in pending],
    )
