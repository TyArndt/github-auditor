"""Who has access to a repo, at what level, and why."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import ProgrammingError

from tests.db.conftest import rows
from tests.db.factories import Seed


def access(db: Connection, repo_id: int) -> dict[str, dict[str, Any]]:
    result = rows(db, "SELECT * FROM reporting.v_repo_access WHERE repo_id = :r", r=repo_id)
    return {r["login"]: r for r in result}


def test_nested_team_members_inherit_parent_access(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    eng = seed.team(org, "eng")
    backend = seed.team(org, "eng-backend", parent=eng)
    api = seed.team(org, "eng-backend-api", parent=backend)  # grandchild
    alice, bob = seed.user("alice_acme"), seed.user("bob_acme")
    seed.team_member(backend, alice)
    seed.team_member(api, bob)
    seed.grant_team(repo, eng, "write")

    a = access(db, repo)
    assert a["alice_acme"]["effective_permission"] == "write"
    assert a["alice_acme"]["sources"] == "team:eng via eng-backend (write)"
    assert a["bob_acme"]["sources"] == "team:eng via eng-backend-api (write)"


def test_highest_permission_wins_and_all_sources_listed(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    team = seed.team(org, "payments")
    alice = seed.user("alice_acme")
    seed.team_member(team, alice)
    seed.grant_team(repo, team, "write")
    seed.grant_user(repo, alice, "admin")

    a = access(db, repo)["alice_acme"]
    assert a["effective_permission"] == "admin"
    assert a["has_direct_grant"] and a["has_team_grant"]
    assert a["sources"] == "direct (admin), team:payments (write)"


def test_custom_role_ranks_by_base_permission(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    alice = seed.user("alice_acme")
    seed.grant_user(repo, alice, "maintain", role_name="release-manager")

    a = access(db, repo)["alice_acme"]
    assert a["effective_permission"] == "maintain"
    assert a["sources"] == "direct (release-manager)"


def test_org_owners_are_admin_everywhere(db: Connection, seed: Seed) -> None:
    org = seed.org()
    r1, r2 = seed.repo(org, "a"), seed.repo(org, "b")
    owner = seed.user("owner_acme")
    seed.org_member(org, owner, role="admin")
    seed.org_member(org, seed.user("member_acme"), role="member")

    for repo in (r1, r2):
        a = access(db, repo)
        assert set(a) == {"owner_acme"}
        assert a["owner_acme"]["is_org_owner"]
        assert a["owner_acme"]["effective_permission"] == "admin"


def test_removed_grants_and_memberships_are_excluded(db: Connection, seed: Seed) -> None:
    org = seed.org()
    repo = seed.repo(org, "svc")
    team = seed.team(org, "old-team")
    alice, bob = seed.user("alice_acme"), seed.user("bob_acme")
    seed.team_member(team, alice)
    seed.grant_team(repo, team, "write", deleted_at=datetime.now(UTC))
    seed.grant_user(repo, bob, "read", deleted_at=datetime.now(UTC))

    assert access(db, repo) == {}


def test_reader_role_sees_views_but_not_tables(db: Connection, seed: Seed) -> None:
    seed.repo(seed.org(), "svc")
    db.exec_driver_sql("SET LOCAL ROLE auditor_read")
    assert rows(db, "SELECT name FROM reporting.v_repositories") == [{"name": "svc"}]

    nested = db.begin_nested()
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.exec_driver_sql("SELECT 1 FROM inventory.repositories")
    nested.rollback()
