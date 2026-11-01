"""End-to-end: fake GitHub -> run_sync -> real PostgreSQL -> reporting views."""

from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from auditor.config import Settings
from auditor.db.migrate import migrate
from auditor.github.auth import AppAuth
from auditor.github.cache import EtagCache
from auditor.github.client import GitHubClient
from auditor.sync import SyncResult, run_sync
from tests.db.conftest import fresh_database
from tests.fake_github import FakeGitHub


@pytest.fixture
def db_url() -> Iterator[str]:
    with fresh_database() as url:  # own database: run_sync commits
        migrate(url)
        yield url


def _sync(fake: FakeGitHub, url: str, private_key_pem: str, **kw: Any) -> SyncResult:
    settings = Settings(
        _env_file=None,
        github_org="acme",
        github_app_id=1,
        github_installation_id=1,
        github_private_key=private_key_pem,
        database_url=url,
    )

    def client_factory(etags: EtagCache) -> GitHubClient:
        http = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler)
        )
        auth = AppAuth(1, 1, private_key_pem, http)
        return GitHubClient(auth, http, etags, sleep=lambda _: None)

    return run_sync(settings, client_factory=client_factory, **kw)


def q(url: str, query: str, **params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(query.encode(), params or None).fetchall()


def test_full_sync_populates_reports(db_url: str, private_key_pem: str) -> None:
    result = _sync(FakeGitHub(), db_url, private_key_pem)
    assert result.status == "success", result.errors
    assert (result.repos_seen, result.repos_deep_synced, result.branches_seen) == (3, 3, 3)

    compliance = {
        r["repository"]: (r["is_compliant"], r["protection_source"], r["failures"])
        for r in q(db_url, "SELECT * FROM reporting.v_default_branch_compliance")
    }
    assert compliance == {
        "acme/svc": (True, "both", []),  # classic rule + org ruleset
        "acme/legacy": (True, "ruleset", []),  # org ruleset via ~DEFAULT_BRANCH
        "acme/empty": (
            False,
            "none",
            [
                "default branch not found",
                "no required review",
                "force push allowed",
                "deletion allowed",
            ],
        ),
    }

    feature = q(db_url, "SELECT * FROM reporting.v_branch_protection WHERE branch = 'feature-x'")
    assert feature[0]["protection_source"] == "none"  # ~DEFAULT_BRANCH doesn't cover it

    access = {
        (r["repository"], r["login"]): (r["effective_permission"], r["sources"])
        for r in q(db_url, "SELECT * FROM reporting.v_repo_access")
    }
    assert access[("acme/svc", "alice_acme")] == ("write", "team:eng via eng-backend (write)")
    assert access[("acme/legacy", "bob_acme")] == ("admin", "direct (admin)")
    assert access[("acme/empty", "owner_acme")] == ("admin", "org_owner:acme (admin)")

    svc = q(db_url, "SELECT * FROM reporting.v_repositories WHERE name = 'svc'")[0]
    assert svc["codeowner_teams"] == ["@acme/eng"]
    assert svc["codeowner_users"] == ["@alice_acme"]
    assert svc["org_base_permission"] == "read"
    assert (svc["workflow_count"], svc["branch_count"], svc["protected_branch_count"]) == (2, 2, 1)

    workflows = {
        r["name"]: (r["last_run_conclusion"], r["runs_last_30d"], r["never_run"])
        for r in q(db_url, "SELECT * FROM reporting.v_workflows")
    }
    assert workflows == {"ci": ("success", 1, False), "nightly": ("failure", 0, False)}

    runs = q(db_url, "SELECT * FROM reporting.v_sync_runs")
    assert runs[0]["status"] == "success" and runs[0]["repos_seen"] == 3
    snapshots = q(db_url, "SELECT count(*) AS n FROM inventory.repo_daily_snapshots")
    assert snapshots[0]["n"] == 3


def test_second_run_soft_deletes_and_uses_etags(db_url: str, private_key_pem: str) -> None:
    fake = FakeGitHub()
    _sync(fake, db_url, private_key_pem)

    fake.branches["svc"] = [b for b in fake.branches["svc"] if b["name"] != "feature-x"]
    fake.collaborators["legacy"] = []
    second = _sync(fake, db_url, private_key_pem)
    assert second.status == "success", second.errors

    branch = q(db_url, "SELECT deleted_at FROM inventory.branches WHERE name = 'feature-x'")
    assert branch[0]["deleted_at"] is not None  # kept for history, marked deleted
    grant = q(db_url, "SELECT deleted_at FROM inventory.repo_user_access")
    assert grant[0]["deleted_at"] is not None
    legacy = q(db_url, "SELECT login FROM reporting.v_repo_access WHERE repository = 'acme/legacy'")
    assert legacy == [{"login": "owner_acme"}]

    run = q(db_url, "SELECT rest_calls FROM inventory.sync_runs ORDER BY id DESC LIMIT 1")[0]
    not_modified = sum(1 for r in fake.requests if r.headers.get("if-none-match"))
    assert run["rest_calls"] > 0 and not_modified > 0  # second run sent conditional requests


def test_limited_run_never_deletes_outside_its_scope(db_url: str, private_key_pem: str) -> None:
    fake = FakeGitHub()
    _sync(fake, db_url, private_key_pem)
    limited = _sync(fake, db_url, private_key_pem, limit=1)  # only "svc" (first listed)
    assert limited.status == "success" and limited.repos_seen == 1

    live = q(db_url, "SELECT name FROM inventory.repositories WHERE deleted_at IS NULL")
    assert {r["name"] for r in live} == {"svc", "legacy", "empty"}
    grants = q(db_url, "SELECT deleted_at FROM inventory.repo_team_access")
    assert all(g["deleted_at"] is None for g in grants)


def test_unreadable_org_ruleset_falls_back_to_branch_rules(
    db_url: str, private_key_pem: str
) -> None:
    """Real-org case: org ruleset details -> 403 'Resource not accessible by integration'."""
    fake = FakeGitHub()
    fake.ruleset_details_forbidden = True
    result = _sync(fake, db_url, private_key_pem)
    assert result.status == "success", result.errors

    compliance = {
        r["repository"]: (r["is_compliant"], r["protection_source"])
        for r in q(db_url, "SELECT * FROM reporting.v_default_branch_compliance")
    }
    assert compliance["acme/svc"] == (True, "both")
    assert compliance["acme/legacy"] == (True, "ruleset")  # rules came from rules/branches

    ruleset = q(db_url, "SELECT raw, conditions FROM inventory.rulesets WHERE id = 9001")[0]
    assert ruleset["raw"]["details_unavailable"] is True
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    rules = q(db_url, "SELECT rule_type FROM inventory.ruleset_rules WHERE ruleset_id = 9001")
    assert {r["rule_type"] for r in rules} == {"pull_request", "non_fast_forward", "deletion"}
    assert any("/rules/branches/" in str(r.url) for r in fake.requests)


def test_one_failing_repo_marks_run_partial(db_url: str, private_key_pem: str) -> None:
    fake = FakeGitHub()
    original = fake._rest

    def broken(path: str, request: httpx.Request) -> Any:
        if path == "/repos/acme/legacy/collaborators":
            raise RuntimeError("boom")
        return original(path, request)

    fake._rest = broken  # type: ignore[method-assign]
    result = _sync(fake, db_url, private_key_pem)
    assert result.status == "partial"
    assert result.repos_deep_synced == 2
    assert result.errors[0]["repo"] == "acme/legacy"
    # nothing org-wide was soft-deleted on a partial run
    assert (
        q(db_url, "SELECT count(*) AS n FROM inventory.users WHERE deleted_at IS NOT NULL")[0]["n"]
        == 0
    )
