"""The metabase/ definitions are consistent (no Metabase needed)."""

from auditor.metabase import DEFINITIONS_DIR, _description, load_definitions

GRID_COLUMNS = 24  # Metabase dashboard grid width


def test_every_card_has_sql_and_every_sql_is_used() -> None:
    _, queries = load_definitions()  # raises on missing files / duplicate names
    sql_files = {p.stem for p in (DEFINITIONS_DIR / "sql").glob("*.sql")}
    assert set(queries) == sql_files, "unused or missing metabase/sql files"


def test_queries_only_read_reporting_views() -> None:
    _, queries = load_definitions()
    for key, sql in queries.items():
        assert "inventory." not in sql, f"{key} reads inventory tables; use reporting views"
        assert "reporting." in sql, f"{key} doesn't read a reporting view"


def test_cards_fit_the_grid_without_overlap() -> None:
    spec, _ = load_definitions()
    for dashboard in spec["dashboards"]:
        occupied: dict[tuple[int, int], str] = {}
        for card in dashboard["cards"]:
            assert card["col"] + card["size_x"] <= GRID_COLUMNS, card["name"]
            for row in range(card["row"], card["row"] + card["size_y"]):
                for col in range(card["col"], card["col"] + card["size_x"]):
                    other = occupied.setdefault((row, col), card["name"])
                    assert other == card["name"], f"{card['name']} overlaps {other}"


def test_dashboard_width_is_valid() -> None:
    spec, _ = load_definitions()
    for dashboard in spec["dashboards"]:
        assert dashboard.get("width", "full") in ("full", "fixed"), dashboard["name"]


def test_leading_comments_become_description() -> None:
    assert _description("-- Line one.\n-- Line two.\nSELECT 1") == "Line one. Line two."
    assert _description("SELECT 1") is None
