"""Tables in the `inventory` schema — the system of record.

Conventions (explained in docs/database-design.md):
- GitHub's numeric id is the primary key wherever GitHub provides one, so renames never
  create duplicate rows. Branches have no GitHub id and use a generated surrogate key.
- `created_at` / `updated_at` / `pushed_at` are GitHub's own timestamps.
  `first_seen_at` / `last_seen_at` / `deleted_at` record what the collector observed.
- Rows are soft-deleted (`deleted_at` set), never physically removed, so history survives.
- Values derivable from other columns (is a branch the default? is it protected?) are NOT
  stored; the reporting views compute them, so they can never drift out of sync.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime

SCHEMA = "inventory"

# Predictable constraint names make migrations reviewable and reversible.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ACTIVE = text("deleted_at IS NULL")  # predicate for partial indexes on soft-deleted tables


def _fk(target: str, **kwargs: Any) -> ForeignKey:
    return ForeignKey(f"{SCHEMA}.{target}", **kwargs)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(Text),
        str: Text,
    }


class Tracked:
    """When the collector first and most recently saw this row."""

    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SoftDeleted(Tracked):
    """Set when the row disappears from GitHub; the row itself is kept for history."""

    deleted_at: Mapped[datetime | None]


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class PermissionLevel(Base):
    """read < triage < write < maintain < admin. `rank` lets SQL pick the highest grant."""

    __tablename__ = "permission_levels"

    name: Mapped[str] = mapped_column(primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger, unique=True)


# ---------------------------------------------------------------------------
# Organization, people, teams
# ---------------------------------------------------------------------------


class Organization(Tracked, Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    login: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str | None]
    # Org-wide baseline every member gets on every repo. NULL = App couldn't read it.
    default_repository_permission: Mapped[str | None] = mapped_column(_fk("permission_levels.name"))
    raw: Mapped[dict[str, Any] | None]


class User(SoftDeleted, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    # Not unique: a login is a label that can be renamed; `id` is the identity.
    login: Mapped[str] = mapped_column(index=True)
    name: Mapped[str | None]
    type: Mapped[str] = mapped_column(server_default="User")  # User | Bot


class OrgMember(SoftDeleted, Base):
    __tablename__ = "org_members"
    __table_args__ = (CheckConstraint("role IN ('admin', 'member')", name="role"),)

    org_id: Mapped[int] = mapped_column(_fk("organizations.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(_fk("users.id"), primary_key=True, index=True)
    role: Mapped[str]  # 'admin' = org owner = implicit admin on every repo


class CustomRepoRole(SoftDeleted, Base):
    """Org-defined repository roles; each maps to a base permission level for ranking."""

    __tablename__ = "custom_repo_roles"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    org_id: Mapped[int] = mapped_column(_fk("organizations.id"))
    name: Mapped[str]
    base_role: Mapped[str] = mapped_column(_fk("permission_levels.name"))
    description: Mapped[str | None]


class Team(SoftDeleted, Base):
    __tablename__ = "teams"
    __table_args__ = (
        # A slug can be reused after a team is deleted, so uniqueness covers live rows only.
        Index(None, "org_id", "slug", unique=True, postgresql_where=ACTIVE),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    org_id: Mapped[int] = mapped_column(_fk("organizations.id"))
    slug: Mapped[str]
    name: Mapped[str]
    description: Mapped[str | None]
    privacy: Mapped[str | None]  # closed | secret
    # Nested teams inherit their parent's repository access.
    parent_team_id: Mapped[int | None] = mapped_column(_fk("teams.id"), index=True)


class TeamMember(SoftDeleted, Base):
    """Immediate membership only; inheritance through nesting is computed in views."""

    __tablename__ = "team_members"
    __table_args__ = (CheckConstraint("role IN ('member', 'maintainer')", name="role"),)

    team_id: Mapped[int] = mapped_column(_fk("teams.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(_fk("users.id"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(server_default="member")


# ---------------------------------------------------------------------------
# Repositories and branches
# ---------------------------------------------------------------------------


class Repository(SoftDeleted, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("visibility IN ('public', 'private', 'internal')", name="visibility"),
        # A deleted repo's name can be reused by a new repo (with a new id).
        Index(None, "org_id", "name", unique=True, postgresql_where=ACTIVE),
        Index(None, "pushed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    org_id: Mapped[int] = mapped_column(_fk("organizations.id"))
    name: Mapped[str]
    full_name: Mapped[str]
    description: Mapped[str | None]
    visibility: Mapped[str]
    is_archived: Mapped[bool] = mapped_column(server_default="false")
    is_disabled: Mapped[bool] = mapped_column(server_default="false")
    is_fork: Mapped[bool] = mapped_column(server_default="false")
    is_template: Mapped[bool] = mapped_column(server_default="false")
    is_empty: Mapped[bool] = mapped_column(server_default="false")
    default_branch: Mapped[str | None]  # NULL for empty repos
    primary_language: Mapped[str | None]
    topics: Mapped[list[str]] = mapped_column(server_default="{}")
    size_kb: Mapped[int | None]
    created_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]
    pushed_at: Mapped[datetime | None]
    custom_properties: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    raw: Mapped[dict[str, Any] | None]
    # When branches/rulesets/workflows were last fully re-read (drives incremental sync).
    details_synced_at: Mapped[datetime | None]


class Branch(SoftDeleted, Base):
    """Is-default and is-protected are computed in reporting views, not stored."""

    __tablename__ = "branches"
    # Full (not partial) unique: a re-created branch revives its old row.
    __table_args__ = (UniqueConstraint("repo_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"))
    name: Mapped[str]
    head_sha: Mapped[str | None]
    last_commit_at: Mapped[datetime | None]
    last_commit_author_login: Mapped[str | None]


# ---------------------------------------------------------------------------
# Branch protection: classic rules and rulesets
# ---------------------------------------------------------------------------


class BranchProtectionRule(SoftDeleted, Base):
    """A classic branch protection rule (pattern-based, one repo)."""

    __tablename__ = "branch_protection_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), index=True)
    pattern: Mapped[str]
    requires_approving_reviews: Mapped[bool] = mapped_column(server_default="false")
    required_approving_review_count: Mapped[int] = mapped_column(server_default="0")
    dismisses_stale_reviews: Mapped[bool] = mapped_column(server_default="false")
    requires_code_owner_reviews: Mapped[bool] = mapped_column(server_default="false")
    require_last_push_approval: Mapped[bool] = mapped_column(server_default="false")
    requires_status_checks: Mapped[bool] = mapped_column(server_default="false")
    requires_strict_status_checks: Mapped[bool] = mapped_column(server_default="false")
    required_status_check_contexts: Mapped[list[str]] = mapped_column(server_default="{}")
    requires_conversation_resolution: Mapped[bool] = mapped_column(server_default="false")
    requires_linear_history: Mapped[bool] = mapped_column(server_default="false")
    requires_commit_signatures: Mapped[bool] = mapped_column(server_default="false")
    is_admin_enforced: Mapped[bool] = mapped_column(server_default="false")
    allows_force_pushes: Mapped[bool] = mapped_column(server_default="false")
    allows_deletions: Mapped[bool] = mapped_column(server_default="false")
    blocks_creations: Mapped[bool] = mapped_column(server_default="false")
    lock_branch: Mapped[bool] = mapped_column(server_default="false")
    raw: Mapped[dict[str, Any] | None]


class Ruleset(SoftDeleted, Base):
    """A repository, organization, or enterprise ruleset."""

    __tablename__ = "rulesets"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('Repository', 'Organization', 'Enterprise')", name="source_type"
        ),
        CheckConstraint("target IN ('branch', 'tag', 'push')", name="target"),
        CheckConstraint("enforcement IN ('active', 'evaluate', 'disabled')", name="enforcement"),
        CheckConstraint(
            "(source_type = 'Repository') = (source_repo_id IS NOT NULL)", name="repo_source"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str | None] = mapped_column(unique=True)
    org_id: Mapped[int] = mapped_column(_fk("organizations.id"))  # org it was collected via
    source_type: Mapped[str]
    source_repo_id: Mapped[int | None] = mapped_column(_fk("repositories.id"))
    name: Mapped[str]
    target: Mapped[str]
    enforcement: Mapped[str]  # only 'active' counts toward protection
    conditions: Mapped[dict[str, Any] | None]
    bypass_actors: Mapped[list[Any]] = mapped_column(server_default="[]")
    created_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]
    raw: Mapped[dict[str, Any] | None]


class RulesetRule(Base):
    """One rule inside a ruleset (e.g. pull_request, deletion, non_fast_forward).

    Replaced wholesale whenever the ruleset is re-read, so no soft delete.
    """

    __tablename__ = "ruleset_rules"
    __table_args__ = (Index(None, "ruleset_id", "rule_type"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ruleset_id: Mapped[int] = mapped_column(_fk("rulesets.id", ondelete="CASCADE"))
    rule_type: Mapped[str]
    parameters: Mapped[dict[str, Any] | None]


class RepoRuleset(SoftDeleted, Base):
    """Which rulesets (including inherited org/enterprise ones) apply to which repo."""

    __tablename__ = "repo_rulesets"

    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), primary_key=True)
    ruleset_id: Mapped[int] = mapped_column(_fk("rulesets.id"), primary_key=True, index=True)


class BranchProtectionMatch(Base):
    """Which classic rule and/or rulesets target each branch. Exactly one FK is set per row.

    Recomputed for a repo's branches every time the repo is synced (delete + insert).
    Ruleset matches include non-active rulesets; views filter on enforcement.
    """

    __tablename__ = "branch_protection_matches"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(protection_rule_id, ruleset_id) = 1", name="exactly_one_source"
        ),
        UniqueConstraint("branch_id", "protection_rule_id"),
        UniqueConstraint("branch_id", "ruleset_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    branch_id: Mapped[int] = mapped_column(_fk("branches.id", ondelete="CASCADE"))
    protection_rule_id: Mapped[int | None] = mapped_column(
        _fk("branch_protection_rules.id", ondelete="CASCADE"), index=True
    )
    ruleset_id: Mapped[int | None] = mapped_column(
        _fk("rulesets.id", ondelete="CASCADE"), index=True
    )
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------------------
# Access: explicit grants. Org baseline and org owners are applied in views.
# ---------------------------------------------------------------------------


class RepoTeamAccess(SoftDeleted, Base):
    __tablename__ = "repo_team_access"

    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(_fk("teams.id"), primary_key=True, index=True)
    permission: Mapped[str] = mapped_column(_fk("permission_levels.name"))  # base level
    role_name: Mapped[str]  # as shown in GitHub; may be a custom role name


class RepoUserAccess(SoftDeleted, Base):
    """Direct collaborator grants only (not via team or org)."""

    __tablename__ = "repo_user_access"

    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(_fk("users.id"), primary_key=True, index=True)
    permission: Mapped[str] = mapped_column(_fk("permission_levels.name"))
    role_name: Mapped[str]


# ---------------------------------------------------------------------------
# CODEOWNERS (default branch)
# ---------------------------------------------------------------------------


class CodeownersFile(Base):
    """One row per repo once checked. `path` NULL means checked and no CODEOWNERS found."""

    __tablename__ = "codeowners_files"

    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), primary_key=True)
    path: Mapped[str | None]  # .github/CODEOWNERS | CODEOWNERS | docs/CODEOWNERS
    ref: Mapped[str | None]
    blob_sha: Mapped[str | None]
    content: Mapped[str | None]
    errors: Mapped[list[Any]] = mapped_column(server_default="[]")
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CodeownersRule(Base):
    """One parsed line: path pattern -> owners (@user, @org/team, or email)."""

    __tablename__ = "codeowners_rules"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    repo_id: Mapped[int] = mapped_column(
        _fk("codeowners_files.repo_id", ondelete="CASCADE"), index=True
    )
    line_number: Mapped[int]
    pattern: Mapped[str]
    owners: Mapped[list[str]]


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


class Workflow(SoftDeleted, Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), index=True)
    name: Mapped[str]
    path: Mapped[str]
    state: Mapped[str]  # active | disabled_manually | disabled_inactivity | ...
    created_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]
    last_run_at: Mapped[datetime | None]
    last_run_status: Mapped[str | None]
    last_run_conclusion: Mapped[str | None]
    runs_last_30d: Mapped[int | None]
    raw: Mapped[dict[str, Any] | None]


# ---------------------------------------------------------------------------
# History and operations
# ---------------------------------------------------------------------------


class SyncRun(Base):
    """Audit log of every collector run."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'success', 'partial', 'failed')", name="status"),
        CheckConstraint("mode IN ('full', 'incremental')", name="mode"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default="running")
    mode: Mapped[str]
    app_version: Mapped[str | None]
    repos_seen: Mapped[int] = mapped_column(server_default="0")
    repos_deep_synced: Mapped[int] = mapped_column(server_default="0")
    branches_seen: Mapped[int] = mapped_column(server_default="0")
    rest_calls: Mapped[int] = mapped_column(server_default="0")
    graphql_points: Mapped[int] = mapped_column(server_default="0")
    errors: Mapped[list[Any]] = mapped_column(server_default="[]")


class RepoDailySnapshot(Base):
    """One small summary row per repo per day — powers trends and point-in-time questions."""

    __tablename__ = "repo_daily_snapshots"

    snapshot_date: Mapped[date] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(_fk("repositories.id"), primary_key=True, index=True)
    sync_run_id: Mapped[int | None] = mapped_column(_fk("sync_runs.id"))
    visibility: Mapped[str]
    is_archived: Mapped[bool]
    days_since_push: Mapped[int | None]
    branch_count: Mapped[int]
    protected_branch_count: Mapped[int]
    default_branch_protected: Mapped[bool]
    default_branch_compliant: Mapped[bool]
    workflow_count: Mapped[int]
    has_codeowners: Mapped[bool]
    team_grant_count: Mapped[int]
    direct_grant_count: Mapped[int]
    admin_user_count: Mapped[int]


class HttpCache(Base):
    """ETag cache: GitHub doesn't count 304 Not Modified responses against the rate limit."""

    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(primary_key=True)
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    body: Mapped[Any] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
