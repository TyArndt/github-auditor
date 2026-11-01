"""Collectors: fetch from GitHub and turn API responses into plain records.

No database code here. `sync.py` runs these (repo details in worker threads) and hands
the records to `load.py`. Each `parse_*` function is pure, so tests can feed it
recorded API responses.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from auditor.codeowners import CODEOWNERS_PATHS
from auditor.github import queries
from auditor.github.client import GitHubClient, NotFoundError

log = logging.getLogger(__name__)

WORKFLOW_RUN_WINDOW_DAYS = 30
MAX_RUN_PAGES = 5  # cap per repo; counts become NULL (unknown) beyond this

# REST `permissions` flags -> our base permission levels, highest first.
_PERMISSION_FLAGS = (
    ("admin", "admin"),
    ("maintain", "maintain"),
    ("push", "write"),
    ("triage", "triage"),
    ("pull", "read"),
)


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def base_permission(permissions: dict[str, bool] | None) -> str:
    for flag, level in _PERMISSION_FLAGS:
        if (permissions or {}).get(flag):
            return level
    return "none"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class UserRec:
    id: int
    node_id: str
    login: str
    name: str | None = None
    type: str = "User"


@dataclass
class OrgRec:
    id: int
    node_id: str
    login: str
    name: str | None
    default_repository_permission: str | None
    raw: dict[str, Any]


@dataclass
class CustomRoleRec:
    id: int
    name: str
    base_role: str
    description: str | None


@dataclass
class TeamRec:
    id: int
    node_id: str
    slug: str
    name: str
    description: str | None
    privacy: str | None
    parent_id: int | None
    members: list[tuple[UserRec, str]] = field(default_factory=list)  # (user, role)


@dataclass
class TeamGrantRec:
    team_id: int
    repo_id: int
    permission: str
    role_name: str


@dataclass
class CodeownersRec:
    path: str | None  # None = checked, not found
    blob_sha: str | None
    content: str | None


@dataclass
class RepoRec:
    id: int
    node_id: str
    name: str
    full_name: str
    description: str | None
    visibility: str
    is_archived: bool
    is_disabled: bool
    is_fork: bool
    is_template: bool
    is_empty: bool
    default_branch: str | None
    primary_language: str | None
    topics: list[str]
    size_kb: int | None
    created_at: datetime | None
    updated_at: datetime | None
    pushed_at: datetime | None
    codeowners: CodeownersRec
    raw: dict[str, Any]
    custom_properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassicRuleRec:
    id: int
    node_id: str
    pattern: str
    settings: dict[str, Any]  # column name -> value
    raw: dict[str, Any]


@dataclass
class BranchRec:
    name: str
    head_sha: str | None
    last_commit_at: datetime | None
    last_commit_author_login: str | None
    classic_rule_id: int | None


@dataclass
class RulesetRec:
    id: int
    node_id: str | None
    source_type: str
    name: str
    target: str
    enforcement: str
    conditions: dict[str, Any] | None
    bypass_actors: list[Any]
    rules: list[tuple[str, dict[str, Any] | None]]
    created_at: datetime | None
    updated_at: datetime | None
    raw: dict[str, Any]


@dataclass
class CollaboratorRec:
    user: UserRec
    permission: str
    role_name: str


@dataclass
class WorkflowRec:
    id: int
    node_id: str
    name: str
    path: str
    state: str
    created_at: datetime | None
    updated_at: datetime | None
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_run_conclusion: str | None = None
    runs_last_30d: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepoDetails:
    repo: RepoRec
    branches: list[BranchRec]
    classic_rules: list[ClassicRuleRec]
    rulesets: list[RulesetRec]  # every ruleset that applies to this repo
    collaborators: list[CollaboratorRec]
    workflows: list[WorkflowRec]


# ---------------------------------------------------------------------------
# Organization, members, roles, teams
# ---------------------------------------------------------------------------


def fetch_org(client: GitHubClient, org: str) -> OrgRec:
    data = client.rest(f"/orgs/{org}")
    return OrgRec(
        id=data["id"],
        node_id=data["node_id"],
        login=data["login"],
        name=data.get("name"),
        default_repository_permission=data.get("default_repository_permission"),
        raw=data,
    )


def _user_from_node(node: dict[str, Any]) -> UserRec:
    return UserRec(
        id=node["databaseId"], node_id=node["id"], login=node["login"], name=node.get("name")
    )


def _user_from_rest(item: dict[str, Any]) -> UserRec:
    return UserRec(
        id=item["id"], node_id=item["node_id"], login=item["login"], type=item.get("type", "User")
    )


def _paginate_graphql(
    client: GitHubClient, query: str, variables: dict[str, Any], path: list[str]
) -> list[dict[str, Any]]:
    """Collect `nodes` or `edges` from a connection found at `path` across all pages."""
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        data: Any = client.graphql(query, {**variables, "cursor": cursor})
        for key in path:
            data = data[key]
        items.extend(data.get("nodes") or data.get("edges") or [])
        if not data["pageInfo"]["hasNextPage"]:
            return items
        cursor = data["pageInfo"]["endCursor"]


def fetch_org_members(client: GitHubClient, org: str) -> list[tuple[UserRec, str]]:
    """(user, 'admin' | 'member'). Admins are org owners."""
    edges = _paginate_graphql(
        client, queries.ORG_MEMBERS, {"org": org}, ["organization", "membersWithRole"]
    )
    return [(_user_from_node(e["node"]), e["role"].lower()) for e in edges]


def fetch_custom_roles(client: GitHubClient, org: str) -> list[CustomRoleRec] | None:
    """None when the org has no custom roles feature or the App can't read them."""
    try:
        data = client.rest(f"/orgs/{org}/custom-repository-roles")
    except NotFoundError:
        return None
    return [
        CustomRoleRec(r["id"], r["name"], r["base_role"], r.get("description"))
        for r in data.get("custom_roles", [])
    ]


def fetch_teams(client: GitHubClient, org: str) -> list[TeamRec]:
    nodes = _paginate_graphql(client, queries.ORG_TEAMS, {"org": org}, ["organization", "teams"])
    teams = []
    for node in nodes:
        members = node["members"]
        edges = list(members["edges"])
        if members["pageInfo"]["hasNextPage"]:  # big team: fetch the rest
            edges = _paginate_graphql(
                client,
                queries.TEAM_MEMBERS,
                {"org": org, "slug": node["slug"]},
                ["organization", "team", "members"],
            )
        teams.append(
            TeamRec(
                id=node["databaseId"],
                node_id=node["id"],
                slug=node["slug"],
                name=node["name"],
                description=node.get("description"),
                privacy=(node.get("privacy") or "").lower() or None,
                parent_id=(node.get("parentTeam") or {}).get("databaseId"),
                members=[(_user_from_node(e["node"]), e["role"].lower()) for e in edges],
            )
        )
    return teams


def fetch_team_grants(client: GitHubClient, org: str, team: TeamRec) -> list[TeamGrantRec]:
    items = client.rest_list(f"/orgs/{org}/teams/{team.slug}/repos")
    return [
        TeamGrantRec(
            team_id=team.id,
            repo_id=item["id"],
            permission=base_permission(item.get("permissions")),
            role_name=item.get("role_name") or base_permission(item.get("permissions")),
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def parse_repo(node: dict[str, Any]) -> RepoRec:
    codeowners = CodeownersRec(None, None, None)
    for path, alias in zip(
        CODEOWNERS_PATHS, ("codeownersGithub", "codeownersRoot", "codeownersDocs"), strict=True
    ):
        blob = node.get(alias)
        if blob and blob.get("text") is not None:
            codeowners = CodeownersRec(path, blob.get("oid"), blob["text"])
            break
    raw = {k: v for k, v in node.items() if not k.startswith("codeowners")}
    return RepoRec(
        id=node["databaseId"],
        node_id=node["id"],
        name=node["name"],
        full_name=node["nameWithOwner"],
        description=node.get("description"),
        visibility=node["visibility"].lower(),
        is_archived=node["isArchived"],
        is_disabled=node["isDisabled"],
        is_fork=node["isFork"],
        is_template=node["isTemplate"],
        is_empty=node["isEmpty"],
        default_branch=(node.get("defaultBranchRef") or {}).get("name"),
        primary_language=(node.get("primaryLanguage") or {}).get("name"),
        topics=[t["topic"]["name"] for t in node["repositoryTopics"]["nodes"]],
        size_kb=node.get("diskUsage"),
        created_at=ts(node.get("createdAt")),
        updated_at=ts(node.get("updatedAt")),
        pushed_at=ts(node.get("pushedAt")),
        codeowners=codeowners,
        raw=raw,
    )


def fetch_repos(client: GitHubClient, org: str, limit: int | None = None) -> list[RepoRec]:
    repos: list[RepoRec] = []
    cursor = None
    while True:
        data = client.graphql(queries.ORG_REPOS, {"org": org, "cursor": cursor})
        page = data["organization"]["repositories"]
        repos.extend(parse_repo(n) for n in page["nodes"] if n)
        if limit is not None and len(repos) >= limit:
            return repos[:limit]
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def fetch_custom_properties(client: GitHubClient, org: str) -> dict[int, dict[str, Any]] | None:
    """repo id -> {property: value}. None if the org/App can't use custom properties."""
    try:
        items = client.rest_list(f"/orgs/{org}/properties/values")
    except NotFoundError:
        return None
    return {
        item["repository_id"]: {p["property_name"]: p["value"] for p in item["properties"]}
        for item in items
    }


# ---------------------------------------------------------------------------
# Per-repository details (run in worker threads)
# ---------------------------------------------------------------------------

_CLASSIC_FIELDS = {
    "requiresApprovingReviews": "requires_approving_reviews",
    "requiredApprovingReviewCount": "required_approving_review_count",
    "dismissesStaleReviews": "dismisses_stale_reviews",
    "requiresCodeOwnerReviews": "requires_code_owner_reviews",
    "requireLastPushApproval": "require_last_push_approval",
    "requiresStatusChecks": "requires_status_checks",
    "requiresStrictStatusChecks": "requires_strict_status_checks",
    "requiredStatusCheckContexts": "required_status_check_contexts",
    "requiresConversationResolution": "requires_conversation_resolution",
    "requiresLinearHistory": "requires_linear_history",
    "requiresCommitSignatures": "requires_commit_signatures",
    "isAdminEnforced": "is_admin_enforced",
    "allowsForcePushes": "allows_force_pushes",
    "allowsDeletions": "allows_deletions",
    "blocksCreations": "blocks_creations",
    "lockBranch": "lock_branch",
}


def parse_classic_rule(node: dict[str, Any]) -> ClassicRuleRec:
    settings = {col: node.get(key) for key, col in _CLASSIC_FIELDS.items()}
    settings["required_approving_review_count"] = settings["required_approving_review_count"] or 0
    settings["required_status_check_contexts"] = settings["required_status_check_contexts"] or []
    for col, value in settings.items():
        if value is None:
            settings[col] = False
    return ClassicRuleRec(node["databaseId"], node["id"], node["pattern"], settings, node)


def parse_branch(node: dict[str, Any]) -> BranchRec:
    target = node.get("target") or {}
    author = target.get("author") or {}
    return BranchRec(
        name=node["name"],
        head_sha=target.get("oid"),
        last_commit_at=ts(target.get("committedDate")),
        last_commit_author_login=(author.get("user") or {}).get("login") or author.get("name"),
        classic_rule_id=(node.get("branchProtectionRule") or {}).get("databaseId"),
    )


def fetch_branches(
    client: GitHubClient, owner: str, name: str
) -> tuple[list[BranchRec], list[ClassicRuleRec]]:
    branches: list[BranchRec] = []
    rules: list[ClassicRuleRec] = []
    cursor = None
    while True:
        data = client.graphql(
            queries.REPO_BRANCHES, {"owner": owner, "name": name, "cursor": cursor}
        )
        repo = data["repository"]
        if cursor is None:
            rules = [parse_classic_rule(n) for n in repo["branchProtectionRules"]["nodes"] if n]
        refs = repo["refs"]
        branches.extend(parse_branch(n) for n in refs["nodes"] if n)
        if not refs["pageInfo"]["hasNextPage"]:
            return branches, rules
        cursor = refs["pageInfo"]["endCursor"]


def parse_ruleset(data: dict[str, Any]) -> RulesetRec:
    return RulesetRec(
        id=data["id"],
        node_id=data.get("node_id"),
        source_type=data["source_type"],
        name=data["name"],
        target=data.get("target") or "branch",
        enforcement=data["enforcement"],
        conditions=data.get("conditions"),
        bypass_actors=data.get("bypass_actors") or [],
        rules=[(r["type"], r.get("parameters")) for r in data.get("rules") or []],
        created_at=ts(data.get("created_at")),
        updated_at=ts(data.get("updated_at")),
        raw=data,
    )


class RulesetFetcher:
    """Fetches ruleset details once per run, however many repos share them.

    Per repo, details are read through the REPOSITORY endpoint
    (/repos/{repo}/rulesets/{id}?includes_parents=true), which also serves inherited
    org/enterprise rulesets and needs only repository-level access.

    Some rulesets can't be read even there (GitHub answers 403 "Resource not accessible by
    integration"; seen on an org ruleset with a required-workflow rule). For those we fall
    back to GitHub's own evaluation of the DEFAULT BRANCH (/repos/{repo}/rules/branches/
    {branch}), which lists each active rule with the ruleset it comes from. Such rulesets
    are recorded with conditions "~DEFAULT_BRANCH" only and raw.details_unavailable = true:
    exact for the default-branch compliance report; other branches can be under-reported.

    Note: GitHub omits `bypass_actors` unless the caller can edit the ruleset, so the
    bypass flag in reports can under-report.
    """

    def __init__(self, client: GitHubClient, org: str) -> None:
        self.client = client
        self.org = org
        self._cache: dict[int, RulesetRec] = {}
        self._lock = threading.Lock()

    def org_rulesets(self) -> list[RulesetRec]:
        """Org-level rulesets, including ones that target no repo yet. Best effort: any the
        App can't read here are still found through each repo they apply to."""
        try:
            summaries = self.client.rest_list(f"/orgs/{self.org}/rulesets")
        except NotFoundError as exc:
            log.warning("can't list org rulesets (HTTP %s); collecting them per repo", exc.status)
            return []
        records = []
        for s in summaries:
            try:
                records.append(self._detail(s["id"], f"/orgs/{self.org}/rulesets/{s['id']}", None))
            except NotFoundError as exc:
                log.warning(
                    "can't read org ruleset %s %r (HTTP %s)", s["id"], s.get("name"), exc.status
                )
        return records

    def for_repo(self, full_name: str, default_branch: str | None) -> list[RulesetRec]:
        summaries = self.client.rest_list(
            f"/repos/{full_name}/rulesets", {"includes_parents": "true"}
        )
        records: list[RulesetRec] = []
        unreadable: list[dict[str, Any]] = []
        for s in summaries:
            try:
                records.append(
                    self._detail(
                        s["id"],
                        f"/repos/{full_name}/rulesets/{s['id']}",
                        {"includes_parents": "true"},
                    )
                )
            except NotFoundError:
                unreadable.append(s)
        if unreadable:
            records += self._from_branch_rules(full_name, default_branch, unreadable)
        return records

    def _from_branch_rules(
        self, full_name: str, default_branch: str | None, summaries: list[dict[str, Any]]
    ) -> list[RulesetRec]:
        """Rebuild unreadable rulesets from the rules GitHub applies to the default branch."""
        names = ", ".join(f"{s['id']} {s.get('name')!r}" for s in summaries)
        if default_branch is None:
            log.warning(
                "%s: can't read rulesets %s and repo has no default branch", full_name, names
            )
            return []
        log.warning(
            "%s: can't read rulesets %s; using rules on %r instead",
            full_name,
            names,
            default_branch,
        )
        applied = self.client.rest_list(f"/repos/{full_name}/rules/branches/{default_branch}")
        records = []
        for s in summaries:
            rules = [
                (r["type"], r.get("parameters")) for r in applied if r.get("ruleset_id") == s["id"]
            ]
            if not rules:
                continue  # doesn't target the default branch; its branch targets are unknown
            records.append(
                RulesetRec(
                    id=s["id"],
                    node_id=s.get("node_id"),
                    source_type=s["source_type"],
                    name=s["name"],
                    target=s.get("target") or "branch",
                    enforcement=s.get("enforcement") or "active",
                    conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                    bypass_actors=[],
                    rules=rules,
                    created_at=ts(s.get("created_at")),
                    updated_at=ts(s.get("updated_at")),
                    raw={
                        "summary": s,
                        "details_unavailable": True,
                        "rules_source": f"rules/branches/{default_branch}",
                    },
                )
            )
        return records

    def _detail(self, ruleset_id: int, path: str, params: dict[str, Any] | None) -> RulesetRec:
        with self._lock:
            cached = self._cache.get(ruleset_id)
        if cached:
            return cached
        record = parse_ruleset(self.client.rest(path, params))
        with self._lock:
            self._cache[record.id] = record
        return record


def fetch_collaborators(client: GitHubClient, full_name: str) -> list[CollaboratorRec]:
    items = client.rest_list(f"/repos/{full_name}/collaborators", {"affiliation": "direct"})
    return [
        CollaboratorRec(
            user=_user_from_rest(item),
            permission=base_permission(item.get("permissions")),
            role_name=item.get("role_name") or base_permission(item.get("permissions")),
        )
        for item in items
    ]


def fetch_workflows(
    client: GitHubClient, full_name: str, now: datetime | None = None
) -> list[WorkflowRec]:
    items = client.rest_list(f"/repos/{full_name}/actions/workflows", key="workflows")
    if not items:
        return []
    workflows = {
        w["id"]: WorkflowRec(
            id=w["id"],
            node_id=w["node_id"],
            name=w["name"],
            path=w["path"],
            state=w["state"],
            created_at=ts(w.get("created_at")),
            updated_at=ts(w.get("updated_at")),
            raw=w,
        )
        for w in items
    }

    # Recent runs (newest first): last run + 30-day count per workflow.
    since = ((now or datetime.now(UTC)) - timedelta(days=WORKFLOW_RUN_WINDOW_DAYS)).date()
    counts: dict[int, int] = {}
    total = 0
    fetched = 0
    for page_number, page in enumerate(
        client.rest_pages(f"/repos/{full_name}/actions/runs", {"created": f">={since}"}), 1
    ):
        total = page.get("total_count", 0)
        for run in page.get("workflow_runs", []):
            fetched += 1
            wf = workflows.get(run["workflow_id"])
            if wf is None:
                continue
            counts[wf.id] = counts.get(wf.id, 0) + 1
            if wf.last_run_at is None:
                wf.last_run_at = ts(run.get("run_started_at") or run.get("created_at"))
                wf.last_run_status = run.get("status")
                wf.last_run_conclusion = run.get("conclusion")
        if page_number >= MAX_RUN_PAGES:
            break
    complete = fetched >= total
    for wf in workflows.values():
        wf.runs_last_30d = counts.get(wf.id, 0) if complete else None

    # Workflows with no run in the window: look up their latest run ever (may be none).
    for wf in workflows.values():
        if wf.last_run_at is None:
            runs = client.rest(
                f"/repos/{full_name}/actions/workflows/{wf.id}/runs", {"per_page": 1}
            ).get("workflow_runs", [])
            if runs:
                wf.last_run_at = ts(runs[0].get("run_started_at") or runs[0].get("created_at"))
                wf.last_run_status = runs[0].get("status")
                wf.last_run_conclusion = runs[0].get("conclusion")
    return list(workflows.values())


def fetch_repo_details(
    client: GitHubClient, repo: RepoRec, rulesets: RulesetFetcher
) -> RepoDetails:
    owner, name = repo.full_name.split("/", 1)
    branches, classic_rules = fetch_branches(client, owner, name)
    return RepoDetails(
        repo=repo,
        branches=branches,
        classic_rules=classic_rules,
        rulesets=rulesets.for_repo(repo.full_name, repo.default_branch),
        collaborators=fetch_collaborators(client, repo.full_name),
        workflows=fetch_workflows(client, repo.full_name),
    )
