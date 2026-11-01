"""Tiny helpers to insert realistic rows with sensible defaults."""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert

from auditor.db import models as m


class Seed:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._ids = itertools.count(1000)

    def _id(self) -> int:
        return next(self._ids)

    def _add(self, model: type[m.Base], **values: Any) -> None:
        self.conn.execute(insert(model).values(**values))

    # -- org & people -------------------------------------------------------

    def org(self, login: str = "acme", base_permission: str | None = "read") -> int:
        oid = self._id()
        self._add(
            m.Organization,
            id=oid,
            node_id=f"O_{oid}",
            login=login,
            default_repository_permission=base_permission,
        )
        return oid

    def user(self, login: str) -> int:
        uid = self._id()
        self._add(m.User, id=uid, node_id=f"U_{uid}", login=login)
        return uid

    def org_member(self, org_id: int, user_id: int, role: str = "member") -> None:
        self._add(m.OrgMember, org_id=org_id, user_id=user_id, role=role)

    def team(self, org_id: int, slug: str, parent: int | None = None) -> int:
        tid = self._id()
        self._add(
            m.Team,
            id=tid,
            node_id=f"T_{tid}",
            org_id=org_id,
            slug=slug,
            name=slug,
            parent_team_id=parent,
        )
        return tid

    def team_member(self, team_id: int, user_id: int) -> None:
        self._add(m.TeamMember, team_id=team_id, user_id=user_id)

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
        self._add(m.Repository, **(values | kw))
        return rid

    def branch(self, repo_id: int, name: str = "main", **kw: Any) -> int:
        values = {"repo_id": repo_id, "name": name} | kw
        return self.conn.execute(
            insert(m.Branch).values(**values).returning(m.Branch.id)
        ).scalar_one()

    # -- protection ---------------------------------------------------------

    def classic_rule(self, repo_id: int, pattern: str = "main", **kw: Any) -> int:
        rid = self._id()
        values: dict[str, Any] = {
            "id": rid,
            "node_id": f"BPR_{rid}",
            "repo_id": repo_id,
            "pattern": pattern,
        }
        self._add(m.BranchProtectionRule, **(values | kw))
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
        self._add(
            m.Ruleset,
            id=rsid,
            org_id=org_id,
            source_type="Organization",
            name=name,
            target="branch",
            enforcement=enforcement,
            bypass_actors=bypass_actors or [],
        )
        for rule_type, params in rules.items():
            self._add(m.RulesetRule, ruleset_id=rsid, rule_type=rule_type, parameters=params)
        return rsid

    def protect(
        self, branch_id: int, *, rule_id: int | None = None, ruleset_id: int | None = None
    ) -> None:
        self._add(
            m.BranchProtectionMatch,
            branch_id=branch_id,
            protection_rule_id=rule_id,
            ruleset_id=ruleset_id,
        )

    # -- access -------------------------------------------------------------

    def grant_team(self, repo_id: int, team_id: int, permission: str, **kw: Any) -> None:
        values = {"repo_id": repo_id, "team_id": team_id, "permission": permission}
        self._add(m.RepoTeamAccess, **({"role_name": permission} | values | kw))

    def grant_user(self, repo_id: int, user_id: int, permission: str, **kw: Any) -> None:
        values = {"repo_id": repo_id, "user_id": user_id, "permission": permission}
        self._add(m.RepoUserAccess, **({"role_name": permission} | values | kw))

    # -- codeowners & workflows --------------------------------------------

    def codeowners(
        self,
        repo_id: int,
        path: str | None,
        rules: list[tuple[str, list[str]]] = (),  # type: ignore[assignment]
    ) -> None:
        self._add(m.CodeownersFile, repo_id=repo_id, path=path, ref="main")
        for line, (pattern, owners) in enumerate(rules, start=1):
            self._add(
                m.CodeownersRule, repo_id=repo_id, line_number=line, pattern=pattern, owners=owners
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
        self._add(m.Workflow, **(values | kw))
        return wid
