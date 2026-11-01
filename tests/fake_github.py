"""An in-memory GitHub that answers the REST + GraphQL calls the collector makes.

Plug it into httpx with `httpx.MockTransport(fake.handler)`. The default scenario:

  org acme (base permission: read); members owner_acme (owner), alice_acme, bob_acme
  repos
    svc     default main; branches main (classic rule: 1 review, no force push/delete)
            and feature-x; CODEOWNERS "* @acme/eng @alice_acme"; workflows ci, nightly
    legacy  default master; no classic rule; bob_acme is a direct admin collaborator
    empty   empty repo, no branches
  org ruleset "baseline" (active, ~DEFAULT_BRANCH): pull_request(1), non_fast_forward, deletion
  teams   eng (granted write on svc) <- child eng-backend (member: alice_acme)

Every GET carries an ETag; If-None-Match with the current ETag returns 304.
`ruleset_details_forbidden = True` makes the baseline ruleset's details return 403 (as seen
in a real org); the collector must then fall back to /rules/branches/{default}.
Tests mutate the scenario between runs (e.g. delete a branch) to test soft deletes.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FORBIDDEN = object()  # _rest() sentinel -> 403 "Resource not accessible by integration"

NOW = datetime.now(UTC)
RECENT = (NOW - timedelta(days=2)).isoformat()
OLD = (NOW - timedelta(days=200)).isoformat()


def _user(uid: int, login: str) -> dict[str, Any]:
    return {"databaseId": uid, "id": f"U_{uid}", "login": login, "name": login.title()}


def _repo_node(rid: int, name: str, default: str | None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "databaseId": rid,
        "id": f"R_{rid}",
        "name": name,
        "nameWithOwner": f"acme/{name}",
        "description": f"{name} service",
        "visibility": "INTERNAL",
        "isArchived": False,
        "isDisabled": False,
        "isFork": False,
        "isTemplate": False,
        "isEmpty": default is None,
        "diskUsage": 120,
        "createdAt": OLD,
        "updatedAt": RECENT,
        "pushedAt": RECENT,
        "primaryLanguage": {"name": "Python"},
        "defaultBranchRef": {"name": default} if default else None,
        "repositoryTopics": {"nodes": [{"topic": {"name": "backend"}}]},
        "codeownersGithub": None,
        "codeownersRoot": None,
        "codeownersDocs": None,
    }
    node.update(extra)
    return node


def _classic_rule(rid: int, pattern: str, **settings: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "databaseId": rid,
        "id": f"BPR_{rid}",
        "pattern": pattern,
        "requiresApprovingReviews": False,
        "requiredApprovingReviewCount": 0,
        "dismissesStaleReviews": False,
        "requiresCodeOwnerReviews": False,
        "requireLastPushApproval": False,
        "requiresStatusChecks": False,
        "requiresStrictStatusChecks": False,
        "requiredStatusCheckContexts": [],
        "requiresConversationResolution": False,
        "requiresLinearHistory": False,
        "requiresCommitSignatures": False,
        "isAdminEnforced": False,
        "allowsForcePushes": False,
        "allowsDeletions": False,
        "blocksCreations": False,
        "lockBranch": False,
    }
    rule.update(settings)
    return rule


def _ref(name: str, sha: str, rule_id: int | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "branchProtectionRule": {"databaseId": rule_id} if rule_id else None,
        "target": {
            "oid": sha,
            "committedDate": RECENT,
            "author": {"name": "Alice", "user": {"login": "alice_acme"}},
        },
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.org = {
            "id": 1,
            "node_id": "O_1",
            "login": "acme",
            "name": "Acme",
            "default_repository_permission": "read",
        }
        self.members = [
            (_user(11, "owner_acme"), "ADMIN"),
            (_user(12, "alice_acme"), "MEMBER"),
            (_user(13, "bob_acme"), "MEMBER"),
        ]
        self.repos = [
            _repo_node(
                101,
                "svc",
                "main",
                codeownersGithub={"oid": "blob1", "text": "# owners\n* @acme/eng @alice_acme\n"},
            ),
            _repo_node(102, "legacy", "master"),
            _repo_node(103, "empty", None),
        ]
        self.classic_rules: dict[str, list[dict[str, Any]]] = {
            "svc": [
                _classic_rule(
                    501, "main", requiresApprovingReviews=True, requiredApprovingReviewCount=1
                )
            ],
        }
        self.branches: dict[str, list[dict[str, Any]]] = {
            "svc": [_ref("main", "a" * 40, 501), _ref("feature-x", "b" * 40)],
            "legacy": [_ref("master", "c" * 40)],
            "empty": [],
        }
        # True reproduces a real-org case: an org ruleset (with a required-workflow rule) whose
        # details return 403 on both the org and repo endpoints.
        self.ruleset_details_forbidden = False
        self.org_ruleset: dict[str, Any] = {
            "id": 9001,
            "node_id": "RRS_9001",
            "name": "baseline",
            "target": "branch",
            "source_type": "Organization",
            "source": "acme",
            "enforcement": "active",
            "conditions": {
                "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
                "repository_name": {"include": ["~ALL"], "exclude": []},
            },
            "rules": [
                {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
                {"type": "non_fast_forward"},
                {"type": "deletion"},
            ],
            "bypass_actors": [],
            "created_at": OLD,
            "updated_at": OLD,
        }
        self.teams: list[dict[str, Any]] = [
            {
                "databaseId": 201,
                "id": "T_201",
                "slug": "eng",
                "name": "Engineering",
                "description": None,
                "privacy": "VISIBLE",
                "parentTeam": None,
                "members": [],
            },
            {
                "databaseId": 202,
                "id": "T_202",
                "slug": "eng-backend",
                "name": "Backend",
                "description": None,
                "privacy": "VISIBLE",
                "parentTeam": {"databaseId": 201},
                "members": [(_user(12, "alice_acme"), "MEMBER")],
            },
        ]
        self.team_repos: dict[str, list[dict[str, Any]]] = {
            "eng": [
                {
                    "id": 101,
                    "full_name": "acme/svc",
                    "role_name": "write",
                    "permissions": {
                        "admin": False,
                        "maintain": False,
                        "push": True,
                        "triage": True,
                        "pull": True,
                    },
                }
            ],
            "eng-backend": [],
        }
        self.collaborators: dict[str, list[dict[str, Any]]] = {
            "legacy": [
                {
                    "id": 13,
                    "node_id": "U_13",
                    "login": "bob_acme",
                    "type": "User",
                    "role_name": "admin",
                    "permissions": {
                        "admin": True,
                        "maintain": True,
                        "push": True,
                        "triage": True,
                        "pull": True,
                    },
                }
            ],
        }
        self.workflows: dict[str, list[dict[str, Any]]] = {
            "svc": [
                {
                    "id": 301,
                    "node_id": "W_301",
                    "name": "ci",
                    "path": ".github/workflows/ci.yml",
                    "state": "active",
                    "created_at": OLD,
                    "updated_at": OLD,
                },
                {
                    "id": 302,
                    "node_id": "W_302",
                    "name": "nightly",
                    "path": ".github/workflows/nightly.yml",
                    "state": "active",
                    "created_at": OLD,
                    "updated_at": OLD,
                },
            ],
        }
        self.recent_runs: dict[str, list[dict[str, Any]]] = {
            "svc": [
                {
                    "workflow_id": 301,
                    "status": "completed",
                    "conclusion": "success",
                    "run_started_at": RECENT,
                    "created_at": RECENT,
                }
            ],
        }
        self.workflow_last_run = {
            302: {
                "workflow_id": 302,
                "status": "completed",
                "conclusion": "failure",
                "run_started_at": OLD,
                "created_at": OLD,
            },
        }

    # -- routing ------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/app/installations/1/access_tokens":
            expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            return httpx.Response(201, json={"token": "ghs_fake", "expires_at": expires})
        if request.method == "POST" and path == "/graphql":
            return httpx.Response(200, json={"data": self._graphql(json.loads(request.content))})
        body = self._rest(path, request)
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if body is FORBIDDEN:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        etag = '"' + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, json=body, headers={"etag": etag})

    def _rest(self, path: str, request: httpx.Request) -> Any:
        repo = re.match(r"^/repos/acme/([^/]+)/(.+)$", path)
        if path == "/orgs/acme":
            return self.org
        if path == "/orgs/acme/custom-repository-roles":
            return {"total_count": 0, "custom_roles": []}
        if path == "/orgs/acme/rulesets":
            return [{k: self.org_ruleset[k] for k in ("id", "name", "source_type", "enforcement")}]
        if path == "/orgs/acme/rulesets/9001":
            return FORBIDDEN if self.ruleset_details_forbidden else self.org_ruleset
        if team := re.match(r"^/orgs/acme/teams/([^/]+)/repos$", path):
            return self.team_repos.get(team.group(1))
        if repo:
            name, rest = repo.groups()
            if rest == "rulesets":
                return [
                    {
                        "id": 9001,
                        "name": "baseline",
                        "source_type": "Organization",
                        "enforcement": "active",
                    }
                ]
            if rest == "rulesets/9001":
                return FORBIDDEN if self.ruleset_details_forbidden else self.org_ruleset
            if branch := re.match(r"^rules/branches/(.+)$", rest):
                return self._branch_rules(name, branch.group(1))
            if rest == "collaborators":
                return self.collaborators.get(name, [])
            if rest == "actions/workflows":
                wfs = self.workflows.get(name, [])
                return {"total_count": len(wfs), "workflows": wfs}
            if rest == "actions/runs":
                runs = self.recent_runs.get(name, [])
                return {"total_count": len(runs), "workflow_runs": runs}
            if wf := re.match(r"^actions/workflows/(\d+)/runs$", rest):
                run = self.workflow_last_run.get(int(wf.group(1)))
                return {"total_count": int(run is not None), "workflow_runs": [run] if run else []}
        return None  # includes /orgs/acme/properties/values -> 404 (feature unavailable)

    def _branch_rules(self, repo_name: str, branch: str) -> list[dict[str, Any]]:
        """GitHub's own evaluation: active rules on a branch, tagged with their ruleset."""
        repo = next(r for r in self.repos if r["name"] == repo_name)
        default = (repo.get("defaultBranchRef") or {}).get("name")
        if branch != default:  # the baseline ruleset targets ~DEFAULT_BRANCH only
            return []
        return [
            {
                "type": rule["type"],
                **({"parameters": rule["parameters"]} if "parameters" in rule else {}),
                "ruleset_source_type": "Organization",
                "ruleset_source": "acme",
                "ruleset_id": self.org_ruleset["id"],
            }
            for rule in self.org_ruleset["rules"]
        ]

    def _graphql(self, payload: dict[str, Any]) -> dict[str, Any]:
        query, variables = payload["query"], payload.get("variables") or {}
        page = {"hasNextPage": False, "endCursor": None}
        rate = {"cost": 1}
        if "query OrgRepos" in query:
            return {
                "rateLimit": rate,
                "organization": {"repositories": {"pageInfo": page, "nodes": self.repos}},
            }
        if "query OrgMembers" in query:
            edges = [{"role": role, "node": user} for user, role in self.members]
            return {
                "rateLimit": rate,
                "organization": {"membersWithRole": {"pageInfo": page, "edges": edges}},
            }
        if "query OrgTeams" in query:
            nodes = []
            for team in self.teams:
                edges = [{"role": role, "node": user} for user, role in team["members"]]
                nodes.append({**team, "members": {"pageInfo": page, "edges": edges}})
            return {
                "rateLimit": rate,
                "organization": {"teams": {"pageInfo": page, "nodes": nodes}},
            }
        if "query RepoBranches" in query:
            name = variables["name"]
            return {
                "rateLimit": rate,
                "repository": {
                    "branchProtectionRules": {"nodes": self.classic_rules.get(name, [])},
                    "refs": {"pageInfo": page, "nodes": self.branches[name]},
                },
            }
        raise AssertionError(f"unexpected GraphQL query: {query[:60]}")
