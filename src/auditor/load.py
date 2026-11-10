"""Write collected records into the `inventory` schema (psycopg, parameterized SQL).

Pattern for every kind of thing (see docs/database-design.md §5):
  1. upsert what GitHub returned: insert new rows, update existing ones, set
     last_seen_at = now() and deleted_at = NULL (revives anything that came back)
  2. soft-delete rows in the same scope that were NOT seen this run, but only when the
     scope was read completely (a failed or partial read never deletes anything)

All methods run on the caller's connection; `sync.py` decides transaction boundaries.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from auditor.codeowners import parse_codeowners
from auditor.collect import (
    DEPENDABOT_SEVERITIES,
    CustomRoleRec,
    DependabotCounts,
    OrgRec,
    RepoDetails,
    RepoRec,
    RulesetRec,
    TeamGrantRec,
    TeamRec,
    UserRec,
)
from auditor.protection import ruleset_targets_branch

Conn = psycopg.Connection[Any]


def _table(name: str) -> sql.Identifier:
    return sql.Identifier("inventory", name)


def upsert(
    conn: Conn,
    table: str,
    rows: Sequence[dict[str, Any]],
    key: Sequence[str],
    *,
    soft_deleted: bool = True,
) -> None:
    """INSERT ... ON CONFLICT (key) DO UPDATE for every row (all rows share the same columns).

    soft_deleted=True: the table has last_seen_at/deleted_at; mark rows seen and revive them.
    """
    if not rows:
        return
    columns = list(rows[0])
    updates: list[sql.Composable] = [
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in columns if c not in key
    ]
    if soft_deleted:
        updates += [sql.SQL("last_seen_at = now()"), sql.SQL("deleted_at = NULL")]
    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({key}) DO UPDATE SET {updates}"
    ).format(
        table=_table(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
        key=sql.SQL(", ").join(map(sql.Identifier, key)),
        updates=sql.SQL(", ").join(updates),
    )
    with conn.cursor() as cur:
        cur.executemany(query, rows)


def soft_delete(conn: Conn, table: str, seen_since: datetime, **scope: Any) -> int:
    """Mark rows in `scope` not seen since `seen_since` as deleted. Returns rows affected."""
    conditions: list[sql.Composable] = [
        sql.SQL("deleted_at IS NULL"),
        sql.SQL("last_seen_at < %(seen_since)s"),
    ]
    conditions += [
        sql.SQL("{c} = {p}").format(c=sql.Identifier(c), p=sql.Placeholder(c)) for c in scope
    ]
    query = sql.SQL("UPDATE {table} SET deleted_at = now() WHERE {where}").format(
        table=_table(table), where=sql.SQL(" AND ").join(conditions)
    )
    return conn.execute(query, {"seen_since": seen_since, **scope}).rowcount


def _jsonb(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


def _dependabot_columns(counts: DependabotCounts | None) -> dict[str, int | None]:
    """repositories.open_dependabot_* columns; all NULL when alerts weren't collected."""
    return {
        "open_dependabot_alert_count": counts.total if counts else None,
        **{
            f"open_dependabot_{severity}": getattr(counts, severity) if counts else None
            for severity in DEPENDABOT_SEVERITIES
        },
    }


@dataclass
class RunInfo:
    id: int
    started_at: datetime


def start_run(conn: Conn, mode: str, app_version: str) -> RunInfo:
    row = conn.execute(
        "INSERT INTO inventory.sync_runs (mode, app_version) VALUES (%s, %s) "
        "RETURNING id, started_at",
        (mode, app_version),
    ).fetchone()
    assert row is not None
    return RunInfo(id=row[0], started_at=row[1])


def finish_run(conn: Conn, run: RunInfo, status: str, errors: list[Any], **counts: int) -> None:
    sets = [sql.SQL("{c} = {p}").format(c=sql.Identifier(c), p=sql.Placeholder(c)) for c in counts]
    query = sql.SQL(
        "UPDATE inventory.sync_runs SET finished_at = now(), status = %(status)s, "
        "errors = %(errors)s{extra} WHERE id = %(id)s"
    ).format(extra=sql.SQL("").join(sql.SQL(", ") + s for s in sets))
    conn.execute(query, {"status": status, "errors": Jsonb(errors), "id": run.id, **counts})


class Loader:
    def __init__(self, conn: Conn, run: RunInfo, *, partial: bool = False) -> None:
        """partial=True (e.g. --limit): only a subset of repos is in this run, so never
        soft-delete anything whose scope reaches beyond the repos actually read."""
        self.conn = conn
        self.run = run
        self.partial = partial
        self.org_id: int | None = None
        self._rulesets_loaded: set[int] = set()

    # -- organization ---------------------------------------------------------

    def org(self, rec: OrgRec) -> None:
        self.org_id = rec.id
        upsert(
            self.conn,
            "organizations",
            [
                {
                    "id": rec.id,
                    "node_id": rec.node_id,
                    "login": rec.login,
                    "name": rec.name,
                    "default_repository_permission": rec.default_repository_permission,
                    "raw": Jsonb(rec.raw),
                }
            ],
            key=["id"],
            soft_deleted=False,
        )

    def users(self, users: Iterable[UserRec]) -> None:
        unique = {u.id: u for u in users}
        if not unique:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO inventory.users (id, node_id, login, name, type)
                VALUES (%(id)s, %(node_id)s, %(login)s, %(name)s, %(type)s)
                ON CONFLICT (id) DO UPDATE SET
                    node_id = EXCLUDED.node_id,
                    login = EXCLUDED.login,
                    name = COALESCE(EXCLUDED.name, users.name),  -- REST lists omit names
                    type = EXCLUDED.type,
                    last_seen_at = now(),
                    deleted_at = NULL
                """,
                [vars(u) for u in unique.values()],
            )

    def org_members(self, members: list[tuple[UserRec, str]]) -> None:
        self.users(u for u, _ in members)
        upsert(
            self.conn,
            "org_members",
            [{"org_id": self.org_id, "user_id": u.id, "role": role} for u, role in members],
            key=["org_id", "user_id"],
        )
        soft_delete(self.conn, "org_members", self.run.started_at, org_id=self.org_id)

    def custom_roles(self, roles: list[CustomRoleRec]) -> None:
        upsert(
            self.conn,
            "custom_repo_roles",
            [
                {
                    "id": r.id,
                    "org_id": self.org_id,
                    "name": r.name,
                    "base_role": r.base_role,
                    "description": r.description,
                }
                for r in roles
            ],
            key=["id"],
        )
        soft_delete(self.conn, "custom_repo_roles", self.run.started_at, org_id=self.org_id)

    # -- repositories (from the org listing) ------------------------------------

    def repos(self, repos: list[RepoRec]) -> None:
        # A repo deleted and re-created with the same name gets a new id: retire the old row
        # first so the "unique live name" index doesn't block the new one.
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE inventory.repositories SET deleted_at = now() "
                "WHERE org_id = %s AND name = %s AND id <> %s AND deleted_at IS NULL",
                [(self.org_id, r.name, r.id) for r in repos],
            )
        upsert(
            self.conn,
            "repositories",
            [
                {
                    "id": r.id,
                    "node_id": r.node_id,
                    "org_id": self.org_id,
                    "name": r.name,
                    "full_name": r.full_name,
                    "description": r.description,
                    "visibility": r.visibility,
                    "is_archived": r.is_archived,
                    "is_disabled": r.is_disabled,
                    "is_fork": r.is_fork,
                    "is_template": r.is_template,
                    "is_empty": r.is_empty,
                    "default_branch": r.default_branch,
                    "primary_language": r.primary_language,
                    "topics": r.topics,
                    "size_kb": r.size_kb,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                    "pushed_at": r.pushed_at,
                    "custom_properties": Jsonb(r.custom_properties),
                    "raw": Jsonb(r.raw),
                    "open_pull_request_count": r.open_pull_request_count,
                    **_dependabot_columns(r.dependabot),
                }
                for r in repos
            ],
            key=["id"],
        )
        for r in repos:
            self._codeowners(r)

    def _codeowners(self, repo: RepoRec) -> None:
        co = repo.codeowners
        upsert(
            self.conn,
            "codeowners_files",
            [
                {
                    "repo_id": repo.id,
                    "path": co.path,
                    "ref": repo.default_branch,
                    "blob_sha": co.blob_sha,
                    "content": co.content,
                    "fetched_at": datetime.now(self.run.started_at.tzinfo),
                }
            ],
            key=["repo_id"],
            soft_deleted=False,
        )
        self.conn.execute("DELETE FROM inventory.codeowners_rules WHERE repo_id = %s", (repo.id,))
        rules = parse_codeowners(co.content) if co.content else []
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO inventory.codeowners_rules (repo_id, line_number, pattern, owners) "
                "VALUES (%s, %s, %s, %s)",
                [(repo.id, r.line_number, r.pattern, r.owners) for r in rules],
            )

    def soft_delete_repos(self) -> int:
        return soft_delete(self.conn, "repositories", self.run.started_at, org_id=self.org_id)

    # -- teams --------------------------------------------------------------------

    def teams(self, teams: list[TeamRec]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE inventory.teams SET deleted_at = now() "
                "WHERE org_id = %s AND slug = %s AND id <> %s AND deleted_at IS NULL",
                [(self.org_id, t.slug, t.id) for t in teams],
            )
        # Parents may be listed after children: insert without parents, then link them.
        upsert(
            self.conn,
            "teams",
            [
                {
                    "id": t.id,
                    "node_id": t.node_id,
                    "org_id": self.org_id,
                    "slug": t.slug,
                    "name": t.name,
                    "description": t.description,
                    "privacy": t.privacy,
                    "parent_team_id": None,
                }
                for t in teams
            ],
            key=["id"],
        )
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE inventory.teams SET parent_team_id = %s WHERE id = %s",
                [(t.parent_id, t.id) for t in teams if t.parent_id is not None],
            )
        soft_delete(self.conn, "teams", self.run.started_at, org_id=self.org_id)

    def team_members(self, team: TeamRec) -> None:
        self.users(u for u, _ in team.members)
        upsert(
            self.conn,
            "team_members",
            [{"team_id": team.id, "user_id": u.id, "role": role} for u, role in team.members],
            key=["team_id", "user_id"],
        )
        soft_delete(self.conn, "team_members", self.run.started_at, team_id=team.id)

    def team_grants(self, team_id: int, grants: list[TeamGrantRec], repo_ids: set[int]) -> None:
        upsert(
            self.conn,
            "repo_team_access",
            [
                {
                    "repo_id": g.repo_id,
                    "team_id": g.team_id,
                    "permission": g.permission,
                    "role_name": g.role_name,
                }
                for g in grants
                if g.repo_id in repo_ids  # ignore repos outside this run (e.g. --limit)
            ],
            key=["repo_id", "team_id"],
        )
        if not self.partial:
            soft_delete(self.conn, "repo_team_access", self.run.started_at, team_id=team_id)

    # -- rulesets -----------------------------------------------------------------

    def rulesets(self, rulesets: list[RulesetRec], source_repo_id: int | None = None) -> None:
        new = [r for r in rulesets if r.id not in self._rulesets_loaded]
        if not new:
            return
        upsert(
            self.conn,
            "rulesets",
            [
                {
                    "id": r.id,
                    "node_id": r.node_id,
                    "org_id": self.org_id,
                    "source_type": r.source_type,
                    "source_repo_id": source_repo_id if r.source_type == "Repository" else None,
                    "name": r.name,
                    "target": r.target,
                    "enforcement": r.enforcement,
                    "conditions": _jsonb(r.conditions),
                    "bypass_actors": Jsonb(r.bypass_actors),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                    "raw": Jsonb(r.raw),
                }
                for r in new
            ],
            key=["id"],
        )
        with self.conn.cursor() as cur:
            cur.executemany(
                "DELETE FROM inventory.ruleset_rules WHERE ruleset_id = %s", [(r.id,) for r in new]
            )
            cur.executemany(
                "INSERT INTO inventory.ruleset_rules (ruleset_id, rule_type, parameters) "
                "VALUES (%s, %s, %s)",
                [(r.id, rule_type, _jsonb(params)) for r in new for rule_type, params in r.rules],
            )
        self._rulesets_loaded.update(r.id for r in new)

    def soft_delete_rulesets(self) -> int:
        return soft_delete(self.conn, "rulesets", self.run.started_at, org_id=self.org_id)

    def soft_delete_users(self) -> int:
        return soft_delete(self.conn, "users", self.run.started_at)

    # -- per-repository details ---------------------------------------------------

    def repo_details(self, d: RepoDetails) -> None:
        repo = d.repo
        seen = self.run.started_at

        upsert(
            self.conn,
            "branch_protection_rules",
            [
                {
                    "id": r.id,
                    "node_id": r.node_id,
                    "repo_id": repo.id,
                    "pattern": r.pattern,
                    **r.settings,
                    "raw": Jsonb(r.raw),
                }
                for r in d.classic_rules
            ],
            key=["id"],
        )
        soft_delete(self.conn, "branch_protection_rules", seen, repo_id=repo.id)

        upsert(
            self.conn,
            "branches",
            [
                {
                    "repo_id": repo.id,
                    "name": b.name,
                    "head_sha": b.head_sha,
                    "last_commit_at": b.last_commit_at,
                    "last_commit_author_login": b.last_commit_author_login,
                }
                for b in d.branches
            ],
            key=["repo_id", "name"],
        )
        soft_delete(self.conn, "branches", seen, repo_id=repo.id)

        self.rulesets(d.rulesets, source_repo_id=repo.id)
        upsert(
            self.conn,
            "repo_rulesets",
            [{"repo_id": repo.id, "ruleset_id": r.id} for r in d.rulesets],
            key=["repo_id", "ruleset_id"],
        )
        soft_delete(self.conn, "repo_rulesets", seen, repo_id=repo.id)

        self._protection_matches(d)

        self.users(c.user for c in d.collaborators)
        upsert(
            self.conn,
            "repo_user_access",
            [
                {
                    "repo_id": repo.id,
                    "user_id": c.user.id,
                    "permission": c.permission,
                    "role_name": c.role_name,
                }
                for c in d.collaborators
            ],
            key=["repo_id", "user_id"],
        )
        soft_delete(self.conn, "repo_user_access", seen, repo_id=repo.id)

        upsert(
            self.conn,
            "workflows",
            [
                {
                    "id": w.id,
                    "node_id": w.node_id,
                    "repo_id": repo.id,
                    "name": w.name,
                    "path": w.path,
                    "state": w.state,
                    "created_at": w.created_at,
                    "updated_at": w.updated_at,
                    "last_run_at": w.last_run_at,
                    "last_run_status": w.last_run_status,
                    "last_run_conclusion": w.last_run_conclusion,
                    "runs_last_30d": w.runs_last_30d,
                    "raw": Jsonb(w.raw),
                }
                for w in d.workflows
            ],
            key=["id"],
        )
        soft_delete(self.conn, "workflows", seen, repo_id=repo.id)

        self.conn.execute(
            "UPDATE inventory.repositories SET details_synced_at = now() WHERE id = %s",
            (repo.id,),
        )

    def _protection_matches(self, d: RepoDetails) -> None:
        """Recompute which classic rule / rulesets target each live branch of this repo."""
        branch_ids = dict(
            self.conn.execute(
                "SELECT name, id FROM inventory.branches WHERE repo_id = %s AND deleted_at IS NULL",
                (d.repo.id,),
            ).fetchall()
        )
        self.conn.execute(
            "DELETE FROM inventory.branch_protection_matches m USING inventory.branches b "
            "WHERE m.branch_id = b.id AND b.repo_id = %s",
            (d.repo.id,),
        )
        rule_ids = {r.id for r in d.classic_rules}
        matches: list[tuple[int, int | None, int | None]] = []
        for branch in d.branches:
            branch_id = branch_ids.get(branch.name)
            if branch_id is None:
                continue
            if branch.classic_rule_id in rule_ids:
                matches.append((branch_id, branch.classic_rule_id, None))
            for rs in d.rulesets:
                spec = {"target": rs.target, "conditions": rs.conditions}
                if ruleset_targets_branch(spec, branch.name, d.repo.default_branch):
                    matches.append((branch_id, None, rs.id))
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO inventory.branch_protection_matches "
                "(branch_id, protection_rule_id, ruleset_id) VALUES (%s, %s, %s)",
                matches,
            )

    # -- history ------------------------------------------------------------------

    def snapshot(self) -> int:
        """Today's summary row per live repo, from the reporting views."""
        return self.conn.execute(
            """
            INSERT INTO inventory.repo_daily_snapshots (
                snapshot_date, repo_id, sync_run_id, visibility, is_archived, days_since_push,
                branch_count, protected_branch_count, default_branch_protected,
                default_branch_compliant, workflow_count, has_codeowners, team_grant_count,
                direct_grant_count, admin_user_count, open_pull_request_count,
                open_dependabot_alert_count, open_dependabot_critical, open_dependabot_high)
            SELECT current_date, repo_id, %(run_id)s, visibility, is_archived, days_since_push,
                   branch_count, protected_branch_count,
                   coalesce(default_branch_protected, false),
                   coalesce(default_branch_compliant, false),
                   workflow_count, has_codeowners, team_grant_count, direct_grant_count,
                   admin_user_count, open_pull_request_count, open_dependabot_alert_count,
                   open_dependabot_critical, open_dependabot_high
            FROM reporting.v_repositories
            ON CONFLICT (snapshot_date, repo_id) DO UPDATE SET
                sync_run_id = EXCLUDED.sync_run_id,
                visibility = EXCLUDED.visibility,
                is_archived = EXCLUDED.is_archived,
                days_since_push = EXCLUDED.days_since_push,
                branch_count = EXCLUDED.branch_count,
                protected_branch_count = EXCLUDED.protected_branch_count,
                default_branch_protected = EXCLUDED.default_branch_protected,
                default_branch_compliant = EXCLUDED.default_branch_compliant,
                workflow_count = EXCLUDED.workflow_count,
                has_codeowners = EXCLUDED.has_codeowners,
                team_grant_count = EXCLUDED.team_grant_count,
                direct_grant_count = EXCLUDED.direct_grant_count,
                admin_user_count = EXCLUDED.admin_user_count,
                open_pull_request_count = EXCLUDED.open_pull_request_count,
                open_dependabot_alert_count = EXCLUDED.open_dependabot_alert_count,
                open_dependabot_critical = EXCLUDED.open_dependabot_critical,
                open_dependabot_high = EXCLUDED.open_dependabot_high
            """,
            {"run_id": self.run.id},
        ).rowcount
