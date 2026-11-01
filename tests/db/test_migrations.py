import shutil
from pathlib import Path

import psycopg
import pytest

from auditor.db.migrate import (
    MIGRATIONS_DIR,
    VIEWS_DIR,
    MigrationError,
    discover,
    migrate,
    status,
    to_psycopg_url,
)
from tests.db.conftest import fresh_database


def _names(url: str, query: str) -> set[str]:
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute(query.encode())}


def test_fresh_database_migrates_and_rerun_is_a_no_op() -> None:
    with fresh_database() as url:
        assert migrate(url) == [m.path.name for m in discover()]
        assert migrate(url) == []
        result = status(url)
        assert result.pending == [] and len(result.applied) == len(discover())


def test_every_view_file_creates_a_view(migrated_url: str) -> None:
    files = sorted(p.stem for p in VIEWS_DIR.glob("*.sql"))
    assert files, "no view files found in db/views"
    views = _names(migrated_url, "SELECT viewname FROM pg_views WHERE schemaname = 'reporting'")
    assert views == {name.split("_", 1)[1] for name in files}  # "01_v_x" -> "v_x"


def test_permission_levels_seeded(migrated_url: str) -> None:
    levels = _names(migrated_url, "SELECT name FROM inventory.permission_levels")
    assert levels == {"none", "read", "triage", "write", "maintain", "admin"}


def _copy_migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


def test_new_migration_is_applied_and_recorded(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    with fresh_database() as url:
        migrate(url, migrations)
        (migrations / "9999_add_note.sql").write_text(
            "ALTER TABLE inventory.repositories ADD COLUMN note text;", encoding="utf-8"
        )
        assert status(url, migrations).pending == ["9999_add_note.sql"]
        assert migrate(url, migrations) == ["9999_add_note.sql"]
        cols = _names(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'inventory' AND table_name = 'repositories'",
        )
        assert "note" in cols


def test_edited_applied_migration_is_refused(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    with fresh_database() as url:
        migrate(url, migrations)
        first = sorted(migrations.glob("*.sql"))[0]
        first.write_text(first.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        with pytest.raises(MigrationError, match="edited after it was applied"):
            migrate(url, migrations)


def test_failed_migration_rolls_back_everything(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    with fresh_database() as url:
        migrate(url, migrations)
        (migrations / "9998_ok.sql").write_text(
            "CREATE TABLE inventory.should_not_exist (id int);", encoding="utf-8"
        )
        (migrations / "9999_broken.sql").write_text("THIS IS NOT SQL;", encoding="utf-8")
        with pytest.raises(psycopg.errors.SyntaxError):
            migrate(url, migrations)
        tables = _names(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'inventory'")
        assert "should_not_exist" not in tables
        views = _names(url, "SELECT viewname FROM pg_views WHERE schemaname = 'reporting'")
        assert views, "views must survive a failed migrate (whole run rolled back)"


def test_bad_filename_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "add_stuff.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="Bad migration filename"):
        discover(tmp_path)


def test_sqlalchemy_style_url_is_accepted() -> None:
    assert to_psycopg_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert to_psycopg_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
