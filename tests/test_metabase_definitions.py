"""The metabase/ definitions are consistent (no Metabase needed)."""

import json
from pathlib import Path
from typing import Any

import pytest

from auditor.metabase import (
    DEFINITIONS_DIR,
    GRID_COLUMNS,
    MetabaseError,
    _description,
    card_height,
    load_definitions,
    stacked_layout,
)


def test_every_card_has_sql_and_every_sql_is_used() -> None:
    _, queries = load_definitions()  # raises on missing files / duplicate names
    sql_files = {p.stem for p in (DEFINITIONS_DIR / "sql").glob("*.sql")}
    assert set(queries) == sql_files, "unused or missing metabase/sql files"


def test_queries_only_read_reporting_views() -> None:
    _, queries = load_definitions()
    for key, sql in queries.items():
        assert "inventory." not in sql, f"{key} reads inventory tables; use reporting views"
        assert "reporting." in sql, f"{key} doesn't read a reporting view"


def test_cards_stack_full_width_in_file_order() -> None:
    cards: list[dict[str, Any]] = [
        {"key": "a", "display": "scalar"},
        {"key": "b", "display": "table", "height": 10},
        {"key": "c", "display": "pie"},
    ]
    assert [(c["key"], row, h) for c, row, h in stacked_layout(cards)] == [
        ("a", 0, 3),
        ("b", 3, 10),
        ("c", 13, 6),
    ]
    assert GRID_COLUMNS == 24


def test_every_card_has_a_positive_height() -> None:
    spec, _ = load_definitions()
    for dashboard in spec["dashboards"]:
        for card in dashboard["cards"]:
            assert card_height(card) > 0, card["name"]


def test_grid_coordinates_are_rejected(tmp_path: Path) -> None:
    spec, queries = load_definitions()
    spec["dashboards"][0]["cards"][0]["size_x"] = 6  # old-style layout
    (tmp_path / "sql").mkdir()
    for key, sql in queries.items():
        (tmp_path / "sql" / f"{key}.sql").write_text(sql, encoding="utf-8")
    (tmp_path / "dashboards.json").write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(MetabaseError, match="stacked full width"):
        load_definitions(tmp_path)


def test_dashboard_width_is_valid() -> None:
    spec, _ = load_definitions()
    for dashboard in spec["dashboards"]:
        assert dashboard.get("width", "full") in ("full", "fixed"), dashboard["name"]


def test_leading_comments_become_description() -> None:
    assert _description("-- Line one.\n-- Line two.\nSELECT 1") == "Line one. Line two."
    assert _description("SELECT 1") is None
