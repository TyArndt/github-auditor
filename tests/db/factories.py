"""Tiny helpers to insert realistic rows with sensible defaults."""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


class Seed:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self._ids = itertools.count(1000)

    def _id(self) -> int:
        return next(self._ids)

    def _insert(self, table: str, returning: str | None = None, **values: Any) -> Any:
        """INSERT with safely quoted identifiers and bound values."""
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals})").format(
            table=sql.Identifier("inventory", table),
            cols=sql.SQL(", ").join(map(sql.Identifier, values)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(values)),
        )
        if returning:
            query += sql.SQL(" RETURNING {}").format(sql.Identifier(returning))
        cur = self.conn.execute(query, list(values.values()))
        if returning:
            row = cur.fetchone()
            assert row is not None
            return row[returning] if isinstance(row, dict) else row[0]
        return None

    # -- org & people -------------------------------------------------------

    def org(self, login: str = "acme", base_permission: str | None = "read") -> int:
        oid = self._id()
        self._insert(
            "organizations",
            id=oid,
            node_id=f"O_{oid}",
            login=login,
            default_repository_permission=base_permission,
        )
        return oid

    def user(self, login: str) -> int:
        uid = self._id()
        self._insert("users", id=uid, node_id=f"U_{uid}", login=login)
        return uid

    def org_member(self, org_id: int, user_id: int, role: str = "member") -> None:
        self._insert("org_members", org_id=org_id, user_id=user_id, role=role)

    def team(self, org_id: int, slug: str, parent: int | None = None) -> int:
        tid = self._id()
        self._insert(
            "teams",
            id=tid,
            node_id=f"T_{tid}",
            org_id=org_id,
            slug=slug,
            name=slug,
            parent_team_id=parent,
        )
        return tid

    def team_member(self, team_id: int, user_id: int) -> None:
        self._insert("team_members", team_id=team_id, user_id=user_id)

    # -- repos & branches ---------------------------------------------------

    def repo(self, org_id: int, name: str, default_branch: str | None = "main", **kw: Any) -> int:
        rid = self._id()
        values: dict[str, Any] = {
            "id": rid,
            "node_id": f"R_{rid}",
            "org_id": org_id,
            "name": name,
            "full_name": f"acme/{name}",
            "visibility": "internal",
            "default_branch": default_branch,
            "pushed_at": datetime.now(UTC) - timedelta(days=3),
        }
        self._insert("repositories", **(values | kw))
        return rid

    def branch(self, repo_id: int, name: str = "main", **kw: Any) -> int:
        branch_id: int = self._insert(
            "branches", returning="id", **({"repo_id": repo_id, "name": name} | kw)
        )
        return branch_id

    # -- protection ---------------------------------------------------------

    def classic_rule(self, repo_id: int, pattern: str = "main", **kw: Any) -> int:
        rid = self._id()
        values: dict[str, Any] = {
            "id": rid,
            "node_id": f"BPR_{rid}",
            "repo_id": repo_id,
            "pattern": pattern,
        }
        self._insert("branch_protection_rules", **(values | kw))
        return rid

    def ruleset(
        self,
        org_id: int,
        name: str,
        rules: dict[str, dict[str, Any] | None],
        enforcement: str = "active",
        bypass_actors: list[Any] | None = None,
    ) -> int:
        rsid = self._id()
        self._insert(
            "rulesets",
            id=rsid,
            org_id=org_id,
            source_type="Organization",
            name=name,
            target="branch",
            enforcement=enforcement,
            bypass_actors=Jsonb(bypass_actors or []),
        )
        for rule_type, params in rules.items():
            self._insert(
                "ruleset_rules",
                ruleset_id=rsid,
                rule_type=rule_type,
                parameters=Jsonb(params) if params is not None else None,
            )
        return rsid

    def protect(
        self, branch_id: int, *, rule_id: int | None = None, ruleset_id: int | None = None
    ) -> None:
        self._insert(
            "branch_protection_matches",
            branch_id=branch_id,
            protection_rule_id=rule_id,
            ruleset_id=ruleset_id,
        )

    # -- access -------------------------------------------------------------

    def grant_team(self, repo_id: int, team_id: int, permission: str, **kw: Any) -> None:
        values: dict[str, Any] = {"repo_id": repo_id, "team_id": team_id, "permission": permission}
        self._insert("repo_team_access", **({"role_name": permission} | values | kw))

    def grant_user(self, repo_id: int, user_id: int, permission: str, **kw: Any) -> None:
        values: dict[str, Any] = {"repo_id": repo_id, "user_id": user_id, "permission": permission}
        self._insert("repo_user_access", **({"role_name": permission} | values | kw))

    # -- codeowners, workflows, snapshots -----------------------------------

    def codeowners(
        self, repo_id: int, path: str | None, rules: list[tuple[str, list[str]]] | None = None
    ) -> None:
        self._insert("codeowners_files", repo_id=repo_id, path=path, ref="main")
        for line, (pattern, owners) in enumerate(rules or [], start=1):
            self._insert(
                "codeowners_rules",
                repo_id=repo_id,
                line_number=line,
                pattern=pattern,
                owners=owners,
            )

    def workflow(self, repo_id: int, name: str, state: str = "active", **kw: Any) -> int:
        wid = self._id()
        values: dict[str, Any] = {
            "id": wid,
            "node_id": f"W_{wid}",
            "repo_id": repo_id,
            "name": name,
            "path": f".github/workflows/{name}.yml",
            "state": state,
        }
        self._insert("workflows", **(values | kw))
        return wid

    def snapshot(self, **values: Any) -> None:
        self._insert("repo_daily_snapshots", **values)
