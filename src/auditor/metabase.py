"""Provision Metabase from the files in metabase/ (dashboards as code).

    auditor metabase setup

Idempotent. On each run it:
  1. completes Metabase's first-time setup (admin user) if not done yet
  2. creates/updates the database connection (read-only login, `reporting` schema only)
  3. creates/updates the "GitHub Auditor" collection, one question per metabase/sql/*.sql,
     and the dashboards in metabase/dashboards.json; questions that are no longer defined
     are archived
Questions and dashboards are matched by name inside the collection, so manual edits to
them in Metabase are overwritten. Change the files instead.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

DEFINITIONS_DIR = Path(__file__).resolve().parents[2] / "metabase"
DATABASE_NAME = "GitHub Auditor"


class MetabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    metabase_url: str = "http://localhost:3000"
    metabase_admin_email: str
    metabase_admin_password: SecretStr
    # How METABASE (inside its container) reaches PostgreSQL, as the read-only login.
    metabase_db_host: str = "db"
    metabase_db_port: int = 5432
    metabase_db_name: str = "auditor"
    metabase_db_user: str = "metabase_reader"
    metabase_db_password: SecretStr = SecretStr("metabase_reader")


@dataclass
class SetupResult:
    url: str
    database_id: int
    collection_id: int
    cards: int
    archived: int
    dashboards: dict[str, int]  # name -> id


class MetabaseError(RuntimeError):
    pass


class Metabase:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, f"/api{path}", **kwargs)
        if response.is_error:
            raise MetabaseError(
                f"{method} /api{path} -> {response.status_code}: {response.text[:500]}"
            )
        return response.json() if response.content else None

    # -- session ------------------------------------------------------------------

    def ensure_setup(self, email: str, password: str) -> None:
        props = self._call("GET", "/session/properties")
        if props.get("has-user-setup"):
            return
        log.info("completing Metabase first-time setup for %s", email)
        self._call(
            "POST",
            "/setup",
            json={
                "token": props["setup-token"],
                "user": {
                    "email": email,
                    "password": password,
                    "first_name": "Auditor",
                    "last_name": "Admin",
                    "site_name": "GitHub Auditor",
                },
                "prefs": {"site_name": "GitHub Auditor", "allow_tracking": False},
            },
        )

    def login(self, email: str, password: str) -> None:
        session = self._call("POST", "/session", json={"username": email, "password": password})
        self.http.headers["X-Metabase-Session"] = session["id"]

    # -- database -----------------------------------------------------------------

    def ensure_database(self, s: MetabaseSettings) -> int:
        details = {
            "host": s.metabase_db_host,
            "port": s.metabase_db_port,
            "dbname": s.metabase_db_name,
            "user": s.metabase_db_user,
            "password": s.metabase_db_password.get_secret_value(),
            "ssl": False,
            "schema-filters-type": "inclusion",
            "schema-filters-patterns": "reporting",
        }
        body = {"engine": "postgres", "name": DATABASE_NAME, "details": details}
        existing = self._call("GET", "/database")
        databases = existing["data"] if isinstance(existing, dict) else existing
        for db in databases:
            if db["name"] == DATABASE_NAME:
                self._call("PUT", f"/database/{db['id']}", json=body)
                database_id = int(db["id"])
                break
        else:
            database_id = int(self._call("POST", "/database", json=body)["id"])
        self._call("POST", f"/database/{database_id}/sync_schema")
        return database_id

    # -- collection, cards, dashboards --------------------------------------------

    def ensure_collection(self, name: str, description: str) -> int:
        for coll in self._call("GET", "/collection"):
            if coll.get("name") == name and not coll.get("archived") and coll.get("id") != "root":
                self._call("PUT", f"/collection/{coll['id']}", json={"description": description})
                return int(coll["id"])
        created = self._call("POST", "/collection", json={"name": name, "description": description})
        return int(created["id"])

    def _collection_items(self, collection_id: int, model: str) -> dict[str, int]:
        items = self._call("GET", f"/collection/{collection_id}/items", params={"models": model})
        rows = items["data"] if isinstance(items, dict) else items
        return {row["name"]: int(row["id"]) for row in rows}

    def upsert_card(
        self,
        collection_id: int,
        database_id: int,
        card: dict[str, Any],
        sql: str,
        existing: dict[str, int],
    ) -> int:
        body = {
            "name": card["name"],
            "description": _description(sql),
            "collection_id": collection_id,
            "display": card["display"],
            "visualization_settings": card.get("visualization_settings", {}),
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {"query": sql, "template-tags": {}},
            },
        }
        if card["name"] in existing:
            card_id = existing[card["name"]]
            self._call("PUT", f"/card/{card_id}", json=body)
            return card_id
        return int(self._call("POST", "/card", json=body)["id"])

    def archive_card(self, card_id: int) -> None:
        self._call("PUT", f"/card/{card_id}", json={"archived": True})

    def upsert_dashboard(
        self,
        collection_id: int,
        dashboard: dict[str, Any],
        card_ids: dict[str, int],
        existing: dict[str, int],
    ) -> int:
        meta = {
            "name": dashboard["name"],
            "description": dashboard.get("description"),
            "collection_id": collection_id,
        }
        if dashboard["name"] in existing:
            dashboard_id = existing[dashboard["name"]]
        else:
            dashboard_id = int(self._call("POST", "/dashboard", json=meta)["id"])
        dashcards = [
            {
                "id": -(i + 1),  # negative ids = new dashcards; the full list replaces the old
                "card_id": card_ids[c["key"]],
                "row": c["row"],
                "col": c["col"],
                "size_x": c["size_x"],
                "size_y": c["size_y"],
                "parameter_mappings": [],
                "visualization_settings": {},
            }
            for i, c in enumerate(dashboard["cards"])
        ]
        self._call("PUT", f"/dashboard/{dashboard_id}", json={**meta, "dashcards": dashcards})
        return dashboard_id


def _description(sql: str) -> str | None:
    """Leading `--` comment lines of a query become the question's description."""
    lines = []
    for line in sql.splitlines():
        if not line.startswith("--"):
            break
        lines.append(line.removeprefix("--").strip())
    return " ".join(lines) or None


def load_definitions(base: Path = DEFINITIONS_DIR) -> tuple[dict[str, Any], dict[str, str]]:
    """dashboards.json plus the SQL of every card it references (validated)."""
    spec = json.loads((base / "dashboards.json").read_text(encoding="utf-8"))
    queries: dict[str, str] = {}
    names: set[str] = set()
    for dashboard in spec["dashboards"]:
        for card in dashboard["cards"]:
            if card["name"] in names:
                raise MetabaseError(f"duplicate question name: {card['name']}")
            names.add(card["name"])
            path = base / "sql" / f"{card['key']}.sql"
            if not path.is_file():
                raise MetabaseError(f"missing query file for card {card['key']}: {path}")
            queries[card["key"]] = path.read_text(encoding="utf-8").strip()
    return spec, queries


def setup(settings: MetabaseSettings, http: httpx.Client | None = None) -> SetupResult:
    spec, queries = load_definitions()
    http = http or httpx.Client(base_url=settings.metabase_url, timeout=60.0)
    mb = Metabase(http)
    email = settings.metabase_admin_email
    password = settings.metabase_admin_password.get_secret_value()

    mb.ensure_setup(email, password)
    mb.login(email, password)
    database_id = mb.ensure_database(settings)
    collection_id = mb.ensure_collection(
        spec["collection"]["name"], spec["collection"]["description"]
    )

    existing_cards = mb._collection_items(collection_id, "card")
    card_ids: dict[str, int] = {}
    for dashboard in spec["dashboards"]:
        for card in dashboard["cards"]:
            card_ids[card["key"]] = mb.upsert_card(
                collection_id, database_id, card, queries[card["key"]], existing_cards
            )
    defined = {c["name"] for d in spec["dashboards"] for c in d["cards"]}
    stale = [cid for name, cid in existing_cards.items() if name not in defined]
    for card_id in stale:
        mb.archive_card(card_id)

    existing_dashboards = mb._collection_items(collection_id, "dashboard")
    dashboards = {
        d["name"]: mb.upsert_dashboard(collection_id, d, card_ids, existing_dashboards)
        for d in spec["dashboards"]
    }
    return SetupResult(
        url=settings.metabase_url,
        database_id=database_id,
        collection_id=collection_id,
        cards=len(card_ids),
        archived=len(stale),
        dashboards=dashboards,
    )
