"""Every Metabase question runs as the read-only role against the real schema.

Catches a view change that would break a dashboard, before anyone opens Metabase.
"""

from datetime import UTC, datetime, timedelta

import pytest

from auditor.metabase import load_definitions
from tests.db.conftest import DB, rows
from tests.db.factories import Seed

_, QUERIES = load_definitions()


@pytest.mark.parametrize("key", sorted(QUERIES))
def test_question_runs_as_reader(key: str, db: DB, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc", pushed_at=datetime.now(UTC) - timedelta(days=400))
    rule = seed.classic_rule(
        repo, requires_approving_reviews=True, required_approving_review_count=1
    )
    seed.protect(seed.branch(repo, "main"), rule_id=rule)
    seed.branch(repo, "old", last_commit_at=datetime.now(UTC) - timedelta(days=200))
    seed.workflow(repo, "ci")
    seed.codeowners(repo, None)
    owner = seed.user("owner_acme")
    seed.org_member(org, owner, role="admin")
    seed.grant_user(repo, seed.user("bob_acme"), "admin")

    db.execute("SET LOCAL ROLE auditor_read")
    rows(db, QUERIES[key])  # must not raise (permission or SQL errors)
