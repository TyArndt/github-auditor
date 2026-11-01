from alembic import command
from sqlalchemy import create_engine, text

from tests.db.conftest import ROOT, alembic_config, fresh_database

VIEW_FILES = sorted(p.stem for p in (ROOT / "db" / "views").glob("*.sql"))


def _names(url: str, sql: str) -> set[str]:
    engine = create_engine(url)
    with engine.connect() as conn:
        result = {r[0] for r in conn.execute(text(sql))}
    engine.dispose()
    return result


def test_upgrade_downgrade_upgrade_round_trip() -> None:
    with fresh_database() as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        tables = _names(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'inventory'")
        assert tables == {"alembic_version"}
        command.upgrade(cfg, "head")


def test_every_view_file_creates_a_view(migrated_url: str) -> None:
    assert VIEW_FILES, "no view files found in db/views"
    views = _names(migrated_url, "SELECT viewname FROM pg_views WHERE schemaname = 'reporting'")
    expected = {name.split("_", 1)[1] for name in VIEW_FILES}  # "01_v_x" -> "v_x"
    assert views == expected


def test_permission_levels_seeded(migrated_url: str) -> None:
    levels = _names(migrated_url, "SELECT name FROM inventory.permission_levels")
    assert levels == {"none", "read", "triage", "write", "maintain", "admin"}
