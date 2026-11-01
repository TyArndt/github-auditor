"""The main inventory report and trends."""

from datetime import UTC, date, datetime

from tests.db.conftest import DB, rows
from tests.db.factories import Seed


def test_repository_rollups(db: DB, seed: Seed) -> None:
    org = seed.org(base_permission="read")
    repo = seed.repo(org, "svc")
    rule = seed.classic_rule(
        repo, requires_approving_reviews=True, required_approving_review_count=1
    )
    seed.protect(seed.branch(repo, "main"), rule_id=rule)
    seed.branch(repo, "feature-x")
    seed.workflow(repo, "ci", last_run_at=datetime.now(UTC))
    seed.workflow(repo, "old", state="disabled_inactivity")
    seed.codeowners(repo, ".github/CODEOWNERS", [("*", ["@acme/payments", "@alice_acme"])])
    team = seed.team(org, "payments")
    seed.grant_team(repo, team, "admin")
    alice = seed.user("alice_acme")
    seed.team_member(team, alice)
    owner = seed.user("owner_acme")
    seed.org_member(org, owner, role="admin")

    r = rows(db, "SELECT * FROM reporting.v_repositories WHERE repo_id = %(r)s", r=repo)[0]
    assert (r["branch_count"], r["protected_branch_count"]) == (2, 1)
    assert r["default_branch_protected"] and r["default_branch_compliant"] is True
    assert (r["workflow_count"], r["active_workflow_count"], r["has_workflows"]) == (2, 1, True)
    assert r["has_codeowners"] and r["codeowners_path"] == ".github/CODEOWNERS"
    assert r["codeowner_teams"] == ["@acme/payments"]
    assert r["codeowner_users"] == ["@alice_acme"]
    assert r["admin_teams"] == ["payments"]
    assert r["admin_user_count"] == 1  # alice via team; org owner not counted
    assert r["org_base_permission"] == "read"
    assert r["days_since_push"] == 3


def test_codeowners_missing_vs_not_checked(db: DB, seed: Seed) -> None:
    org = seed.org()
    checked = seed.repo(org, "checked")
    seed.codeowners(checked, None)
    unchecked = seed.repo(org, "unchecked")

    result = {
        r["name"]: (r["codeowners_checked"], r["has_codeowners"])
        for r in rows(db, "SELECT * FROM reporting.v_repositories")
    }
    assert result == {"checked": (True, False), "unchecked": (False, False)}
    assert unchecked


def test_daily_trend_excludes_archived_from_compliance(db: DB, seed: Seed) -> None:
    org = seed.org()
    day = date(2026, 11, 1)
    base = {
        "snapshot_date": day,
        "visibility": "internal",
        "days_since_push": 1,
        "branch_count": 1,
        "protected_branch_count": 1,
        "default_branch_protected": True,
        "workflow_count": 0,
        "has_codeowners": False,
        "team_grant_count": 0,
        "direct_grant_count": 0,
        "admin_user_count": 0,
    }
    snaps = [
        (seed.repo(org, "a"), False, True),
        (seed.repo(org, "b"), False, False),
        (seed.repo(org, "c"), True, False),  # archived: excluded from %
    ]
    for repo_id, archived, compliant in snaps:
        seed.snapshot(
            **base, repo_id=repo_id, is_archived=archived, default_branch_compliant=compliant
        )

    t = rows(db, "SELECT * FROM reporting.v_daily_trends")[0]
    assert (t["repo_count"], t["active_repo_count"], t["compliant_count"]) == (3, 2, 1)
    assert float(t["compliance_pct"]) == 50.0
