"""Default-branch compliance: >= 1 review, force push blocked, deletion blocked."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from tests.db.conftest import rows
from tests.db.factories import Seed

COMPLIANT_RULES: dict[str, dict[str, Any] | None] = {
    "pull_request": {"required_approving_review_count": 1},
    "non_fast_forward": None,
    "deletion": None,
}


def compliance(db: Connection, repo_id: int) -> dict[str, Any]:
    return rows(
        db, "SELECT * FROM reporting.v_default_branch_compliance WHERE repo_id = :r", r=repo_id
    )[0]


def test_classic_rule_meeting_minimum_is_compliant(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rule = seed.classic_rule(
        repo, requires_approving_reviews=True, required_approving_review_count=2
    )
    seed.protect(seed.branch(repo), rule_id=rule)

    c = compliance(db, repo)
    assert c["is_compliant"] and c["failures"] == []
    assert c["protection_source"] == "classic" and c["required_reviews"] == 2


def test_classic_rule_allowing_force_push_fails(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rule = seed.classic_rule(
        repo,
        requires_approving_reviews=True,
        required_approving_review_count=1,
        allows_force_pushes=True,
    )
    seed.protect(seed.branch(repo), rule_id=rule)

    c = compliance(db, repo)
    assert not c["is_compliant"]
    assert c["failures"] == ["force push allowed"]


def test_review_count_ignored_when_reviews_not_required(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rule = seed.classic_rule(
        repo, requires_approving_reviews=False, required_approving_review_count=3
    )
    seed.protect(seed.branch(repo), rule_id=rule)

    assert compliance(db, repo)["failures"] == ["no required review"]


def test_unprotected_default_branch_lists_every_failure(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    seed.branch(repo)

    c = compliance(db, repo)
    assert c["protection_source"] == "none" and not c["is_protected"]
    assert c["failures"] == ["no required review", "force push allowed", "deletion allowed"]


def test_active_ruleset_alone_is_compliant(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rs = seed.ruleset(org, "baseline", COMPLIANT_RULES)
    seed.protect(seed.branch(repo), ruleset_id=rs)

    c = compliance(db, repo)
    assert c["is_compliant"] and c["protection_source"] == "ruleset"


def test_evaluate_mode_ruleset_does_not_count(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rs = seed.ruleset(org, "dry-run", COMPLIANT_RULES, enforcement="evaluate")
    seed.protect(seed.branch(repo), ruleset_id=rs)

    c = compliance(db, repo)
    assert not c["is_compliant"] and c["protection_source"] == "none"


def test_classic_and_ruleset_combine_most_restrictive(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    # Classic gives the review; ruleset blocks force push + deletion. Together: compliant.
    rule = seed.classic_rule(
        repo,
        requires_approving_reviews=True,
        required_approving_review_count=1,
        allows_force_pushes=True,
        allows_deletions=True,
    )
    rs = seed.ruleset(org, "no-rewrite", {"non_fast_forward": None, "deletion": None})
    branch = seed.branch(repo)
    seed.protect(branch, rule_id=rule)
    seed.protect(branch, ruleset_id=rs)

    c = compliance(db, repo)
    assert c["is_compliant"] and c["protection_source"] == "both"


def test_bypass_actors_are_informational_only(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    rs = seed.ruleset(org, "baseline", COMPLIANT_RULES, bypass_actors=[{"actor_type": "Team"}])
    seed.protect(seed.branch(repo), ruleset_id=rs)

    c = compliance(db, repo)
    assert c["is_compliant"] and c["ruleset_has_bypass_actors"]


def test_empty_repo_reports_missing_default_branch(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "empty", default_branch=None, is_empty=True)

    assert compliance(db, repo)["failures"][0] == "default branch not found"


def test_deleted_branches_and_repos_are_excluded(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    seed.branch(repo, "main")
    seed.branch(repo, "old-feature", deleted_at=datetime.now(UTC))
    gone = seed.repo(org, "gone", deleted_at=datetime.now(UTC))
    seed.branch(gone)

    branches = rows(db, "SELECT repository, branch FROM reporting.v_branch_protection")
    assert branches == [{"repository": "acme/svc", "branch": "main"}]
    assert (
        rows(db, "SELECT 1 FROM reporting.v_default_branch_compliance WHERE repo_id = :r", r=gone)
        == []
    )
