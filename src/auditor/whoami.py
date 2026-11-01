"""`auditor whoami`: prove the GitHub App is set up correctly before a real sync."""

from dataclasses import dataclass, field
from typing import Any

from auditor.config import Settings
from auditor.github.auth import AppAuth
from auditor.github.client import GitHubClient, make_http

# Installation permissions the collector needs (all read-only).
REQUIRED_PERMISSIONS = {
    "metadata": "read",
    "contents": "read",
    "administration": "read",
    "actions": "read",
    "members": "read",
    "organization_administration": "read",
}


@dataclass
class WhoAmI:
    app_name: str
    app_slug: str
    account: str
    repository_selection: str
    repo_count: int
    org_login: str
    org_base_permission: str | None
    rate_limits: dict[str, dict[str, int]]
    permissions: dict[str, str]
    problems: list[str] = field(default_factory=list)


def whoami(settings: Settings, http: Any = None) -> WhoAmI:
    http = http or make_http(
        settings.github_api_url, settings.github_api_version, settings.ca_bundle
    )
    auth = AppAuth(
        settings.github_app_id, settings.github_installation_id, settings.private_key_pem(), http
    )
    jwt_headers = {"Authorization": f"Bearer {auth.app_jwt()}"}

    app = http.get("/app", headers=jwt_headers)
    app.raise_for_status()
    installation = http.get(
        f"/app/installations/{settings.github_installation_id}", headers=jwt_headers
    )
    installation.raise_for_status()
    inst = installation.json()

    client = GitHubClient(auth, http)
    repos = client.rest("/installation/repositories", {"per_page": 1})
    org = client.rest(f"/orgs/{settings.github_org}")
    limits = client.rate_limits()

    permissions: dict[str, str] = inst.get("permissions", {})
    problems = []
    for name, level in REQUIRED_PERMISSIONS.items():
        if permissions.get(name) not in (level, "write"):
            problems.append(f"missing permission {name}: {level}")
    if inst.get("repository_selection") != "all":
        problems.append("App is installed on SELECTED repositories; choose 'All repositories'")
    if inst.get("account", {}).get("login", "").lower() != settings.github_org.lower():
        problems.append(
            f"installation belongs to {inst.get('account', {}).get('login')}, "
            f"not {settings.github_org}"
        )
    if org.get("default_repository_permission") is None:
        problems.append("can't read org base permission (needs organization_administration: read)")

    return WhoAmI(
        app_name=app.json()["name"],
        app_slug=app.json()["slug"],
        account=inst.get("account", {}).get("login", "?"),
        repository_selection=inst.get("repository_selection", "?"),
        repo_count=repos.get("total_count", 0),
        org_login=org["login"],
        org_base_permission=org.get("default_repository_permission"),
        rate_limits={
            k: {"limit": v["limit"], "remaining": v["remaining"]}
            for k, v in limits.items()
            if k in ("core", "graphql")
        },
        permissions=permissions,
        problems=problems,
    )
