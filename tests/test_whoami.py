from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from auditor.config import Settings
from auditor.whoami import REQUIRED_PERMISSIONS, whoami


def _settings(pem: str) -> Settings:
    return Settings(
        _env_file=None,
        github_org="acme",
        github_app_id=7,
        github_installation_id=9,
        github_private_key=pem,
        database_url="postgresql://x@localhost/x",
    )


def _http(installation: dict[str, Any], base_permission: str | None = "read") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        routes: dict[str, Any] = {
            "/app": {"name": "Acme Repo Auditor", "slug": "acme-repo-auditor"},
            "/app/installations/9": installation,
            "/app/installations/9/access_tokens": {"token": "ghs_x", "expires_at": expires},
            "/installation/repositories": {"total_count": 2534, "repositories": []},
            "/orgs/acme": {"login": "acme", "default_repository_permission": base_permission},
            "/rate_limit": {
                "resources": {
                    "core": {"limit": 15000, "remaining": 14990},
                    "graphql": {"limit": 10000, "remaining": 10000},
                }
            },
        }
        return httpx.Response(200, json=routes[path])

    return httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))


def test_correct_setup_has_no_problems(private_key_pem: str) -> None:
    inst = {
        "account": {"login": "acme"},
        "repository_selection": "all",
        "permissions": dict(REQUIRED_PERMISSIONS),
    }
    info = whoami(_settings(private_key_pem), http=_http(inst))
    assert info.problems == []
    assert (info.repo_count, info.rate_limits["core"]["limit"]) == (2534, 15000)


def test_missing_permission_and_selected_repos_are_reported(private_key_pem: str) -> None:
    perms = {k: v for k, v in REQUIRED_PERMISSIONS.items() if k != "administration"}
    inst = {"account": {"login": "acme"}, "repository_selection": "selected", "permissions": perms}
    info = whoami(_settings(private_key_pem), http=_http(inst, base_permission=None))
    assert "missing permission administration: read" in info.problems
    assert any("SELECTED" in p for p in info.problems)
    assert any("base permission" in p for p in info.problems)
