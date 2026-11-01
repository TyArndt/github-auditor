-- 0001 — Initial inventory schema.
-- Design rationale: docs/database-design.md
--
-- Conventions:
--   * GitHub's numeric id is the primary key wherever GitHub provides one (renames never
--     create duplicates). Branches have no GitHub id and use a generated identity key.
--   * created_at / updated_at / pushed_at are GitHub's timestamps. first_seen_at /
--     last_seen_at / deleted_at record what the collector observed.
--   * Rows are soft-deleted (deleted_at set), never physically removed.
--   * Values derivable from other columns (is a branch default? protected?) are NOT stored;
--     the reporting views compute them.
--   * Constraint names: pk_<table>, uq_<table>_<cols>, fk_<table>_<col>_<ref>,
--     ck_<table>_<rule>, ix_<table>_<cols>.

-- ---------------------------------------------------------------------------
-- Lookups
-- ---------------------------------------------------------------------------

-- read < triage < write < maintain < admin. `rank` lets SQL pick the highest grant.
CREATE TABLE inventory.permission_levels (
    name text     NOT NULL,
    rank smallint NOT NULL,
    CONSTRAINT pk_permission_levels PRIMARY KEY (name),
    CONSTRAINT uq_permission_levels_rank UNIQUE (rank)
);

INSERT INTO inventory.permission_levels (name, rank) VALUES
    ('none', 0), ('read', 1), ('triage', 2), ('write', 3), ('maintain', 4), ('admin', 5);

-- ---------------------------------------------------------------------------
-- Organization, people, teams
-- ---------------------------------------------------------------------------

CREATE TABLE inventory.organizations (
    id            bigint      NOT NULL,
    node_id       text        NOT NULL,
    login         text        NOT NULL,
    name          text,
    -- Org-wide baseline every member gets on every repo. NULL = App couldn't read it.
    default_repository_permission text,
    raw           jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_organizations PRIMARY KEY (id),
    CONSTRAINT uq_organizations_node_id UNIQUE (node_id),
    CONSTRAINT uq_organizations_login UNIQUE (login),
    CONSTRAINT fk_organizations_default_repository_permission
        FOREIGN KEY (default_repository_permission) REFERENCES inventory.permission_levels (name)
);

CREATE TABLE inventory.users (
    id            bigint      NOT NULL,
    node_id       text        NOT NULL,
    login         text        NOT NULL,  -- not unique: logins can be renamed; id is identity
    name          text,
    type          text        NOT NULL DEFAULT 'User',  -- User | Bot
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_node_id UNIQUE (node_id)
);
CREATE INDEX ix_users_login ON inventory.users (login);

-- role 'admin' = org owner = implicit admin on every repo.
CREATE TABLE inventory.org_members (
    org_id        bigint      NOT NULL,
    user_id       bigint      NOT NULL,
    role          text        NOT NULL,
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_org_members PRIMARY KEY (org_id, user_id),
    CONSTRAINT ck_org_members_role CHECK (role IN ('admin', 'member')),
    CONSTRAINT fk_org_members_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES inventory.organizations (id),
    CONSTRAINT fk_org_members_user_id_users FOREIGN KEY (user_id) REFERENCES inventory.users (id)
);
CREATE INDEX ix_org_members_user_id ON inventory.org_members (user_id);

-- Org-defined repository roles; base_role lets them be ranked.
CREATE TABLE inventory.custom_repo_roles (
    id            bigint      NOT NULL,
    org_id        bigint      NOT NULL,
    name          text        NOT NULL,
    base_role     text        NOT NULL,
    description   text,
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_custom_repo_roles PRIMARY KEY (id),
    CONSTRAINT uq_custom_repo_roles_org_id_name UNIQUE (org_id, name),
    CONSTRAINT fk_custom_repo_roles_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES inventory.organizations (id),
    CONSTRAINT fk_custom_repo_roles_base_role_permission_levels
        FOREIGN KEY (base_role) REFERENCES inventory.permission_levels (name)
);

-- Nested teams inherit their parent's repository access (computed in views).
CREATE TABLE inventory.teams (
    id             bigint      NOT NULL,
    node_id        text        NOT NULL,
    org_id         bigint      NOT NULL,
    slug           text        NOT NULL,
    name           text        NOT NULL,
    description    text,
    privacy        text,  -- closed | secret
    parent_team_id bigint,
    deleted_at     timestamptz,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_teams PRIMARY KEY (id),
    CONSTRAINT uq_teams_node_id UNIQUE (node_id),
    CONSTRAINT fk_teams_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES inventory.organizations (id),
    CONSTRAINT fk_teams_parent_team_id_teams
        FOREIGN KEY (parent_team_id) REFERENCES inventory.teams (id)
);
-- A slug can be reused after a team is deleted, so uniqueness covers live rows only.
CREATE UNIQUE INDEX ix_teams_org_id_slug ON inventory.teams (org_id, slug)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_teams_parent_team_id ON inventory.teams (parent_team_id);

-- Immediate membership only; inheritance through nesting is computed in views.
CREATE TABLE inventory.team_members (
    team_id       bigint      NOT NULL,
    user_id       bigint      NOT NULL,
    role          text        NOT NULL DEFAULT 'member',
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_team_members PRIMARY KEY (team_id, user_id),
    CONSTRAINT ck_team_members_role CHECK (role IN ('member', 'maintainer')),
    CONSTRAINT fk_team_members_team_id_teams FOREIGN KEY (team_id) REFERENCES inventory.teams (id),
    CONSTRAINT fk_team_members_user_id_users FOREIGN KEY (user_id) REFERENCES inventory.users (id)
);
CREATE INDEX ix_team_members_user_id ON inventory.team_members (user_id);

-- ---------------------------------------------------------------------------
-- Repositories and branches
-- ---------------------------------------------------------------------------

CREATE TABLE inventory.repositories (
    id                bigint      NOT NULL,
    node_id           text        NOT NULL,
    org_id            bigint      NOT NULL,
    name              text        NOT NULL,
    full_name         text        NOT NULL,
    description       text,
    visibility        text        NOT NULL,
    is_archived       boolean     NOT NULL DEFAULT false,
    is_disabled       boolean     NOT NULL DEFAULT false,
    is_fork           boolean     NOT NULL DEFAULT false,
    is_template       boolean     NOT NULL DEFAULT false,
    is_empty          boolean     NOT NULL DEFAULT false,
    default_branch    text,  -- NULL for empty repos
    primary_language  text,
    topics            text[]      NOT NULL DEFAULT '{}',
    size_kb           integer,
    created_at        timestamptz,
    updated_at        timestamptz,
    pushed_at         timestamptz,
    custom_properties jsonb       NOT NULL DEFAULT '{}',
    raw               jsonb,
    -- When branches/rulesets/workflows were last fully re-read (drives incremental sync).
    details_synced_at timestamptz,
    deleted_at        timestamptz,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_repositories PRIMARY KEY (id),
    CONSTRAINT uq_repositories_node_id UNIQUE (node_id),
    CONSTRAINT ck_repositories_visibility CHECK (visibility IN ('public', 'private', 'internal')),
    CONSTRAINT fk_repositories_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES inventory.organizations (id)
);
-- A deleted repo's name can be reused by a new repo (with a new id).
CREATE UNIQUE INDEX ix_repositories_org_id_name ON inventory.repositories (org_id, name)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_repositories_pushed_at ON inventory.repositories (pushed_at);

-- Full (not partial) unique on (repo_id, name): a re-created branch revives its old row.
CREATE TABLE inventory.branches (
    id                       bigint GENERATED BY DEFAULT AS IDENTITY,
    repo_id                  bigint      NOT NULL,
    name                     text        NOT NULL,
    head_sha                 text,
    last_commit_at           timestamptz,
    last_commit_author_login text,
    deleted_at               timestamptz,
    first_seen_at            timestamptz NOT NULL DEFAULT now(),
    last_seen_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_branches PRIMARY KEY (id),
    CONSTRAINT uq_branches_repo_id_name UNIQUE (repo_id, name),
    CONSTRAINT fk_branches_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id)
);

-- ---------------------------------------------------------------------------
-- Branch protection: classic rules and rulesets
-- ---------------------------------------------------------------------------

CREATE TABLE inventory.branch_protection_rules (
    id                               bigint      NOT NULL,
    node_id                          text        NOT NULL,
    repo_id                          bigint      NOT NULL,
    pattern                          text        NOT NULL,
    requires_approving_reviews       boolean     NOT NULL DEFAULT false,
    required_approving_review_count  integer     NOT NULL DEFAULT 0,
    dismisses_stale_reviews          boolean     NOT NULL DEFAULT false,
    requires_code_owner_reviews      boolean     NOT NULL DEFAULT false,
    require_last_push_approval       boolean     NOT NULL DEFAULT false,
    requires_status_checks           boolean     NOT NULL DEFAULT false,
    requires_strict_status_checks    boolean     NOT NULL DEFAULT false,
    required_status_check_contexts   text[]      NOT NULL DEFAULT '{}',
    requires_conversation_resolution boolean     NOT NULL DEFAULT false,
    requires_linear_history          boolean     NOT NULL DEFAULT false,
    requires_commit_signatures       boolean     NOT NULL DEFAULT false,
    is_admin_enforced                boolean     NOT NULL DEFAULT false,
    allows_force_pushes              boolean     NOT NULL DEFAULT false,
    allows_deletions                 boolean     NOT NULL DEFAULT false,
    blocks_creations                 boolean     NOT NULL DEFAULT false,
    lock_branch                      boolean     NOT NULL DEFAULT false,
    raw                              jsonb,
    deleted_at                       timestamptz,
    first_seen_at                    timestamptz NOT NULL DEFAULT now(),
    last_seen_at                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_branch_protection_rules PRIMARY KEY (id),
    CONSTRAINT uq_branch_protection_rules_node_id UNIQUE (node_id),
    CONSTRAINT fk_branch_protection_rules_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id)
);
CREATE INDEX ix_branch_protection_rules_repo_id ON inventory.branch_protection_rules (repo_id);

-- Repository, organization, or enterprise ruleset. Only enforcement = 'active' counts.
CREATE TABLE inventory.rulesets (
    id             bigint      NOT NULL,
    node_id        text,
    org_id         bigint      NOT NULL,  -- org it was collected via
    source_type    text        NOT NULL,
    source_repo_id bigint,
    name           text        NOT NULL,
    target         text        NOT NULL,
    enforcement    text        NOT NULL,
    conditions     jsonb,
    bypass_actors  jsonb       NOT NULL DEFAULT '[]',
    created_at     timestamptz,
    updated_at     timestamptz,
    raw            jsonb,
    deleted_at     timestamptz,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_rulesets PRIMARY KEY (id),
    CONSTRAINT uq_rulesets_node_id UNIQUE (node_id),
    CONSTRAINT ck_rulesets_source_type
        CHECK (source_type IN ('Repository', 'Organization', 'Enterprise')),
    CONSTRAINT ck_rulesets_target CHECK (target IN ('branch', 'tag', 'push')),
    CONSTRAINT ck_rulesets_enforcement CHECK (enforcement IN ('active', 'evaluate', 'disabled')),
    CONSTRAINT ck_rulesets_repo_source
        CHECK ((source_type = 'Repository') = (source_repo_id IS NOT NULL)),
    CONSTRAINT fk_rulesets_org_id_organizations
        FOREIGN KEY (org_id) REFERENCES inventory.organizations (id),
    CONSTRAINT fk_rulesets_source_repo_id_repositories
        FOREIGN KEY (source_repo_id) REFERENCES inventory.repositories (id)
);

-- One rule inside a ruleset (pull_request, deletion, non_fast_forward, ...).
-- Replaced wholesale whenever the ruleset is re-read, so no soft delete.
CREATE TABLE inventory.ruleset_rules (
    id         bigint GENERATED BY DEFAULT AS IDENTITY,
    ruleset_id bigint NOT NULL,
    rule_type  text   NOT NULL,
    parameters jsonb,
    CONSTRAINT pk_ruleset_rules PRIMARY KEY (id),
    CONSTRAINT fk_ruleset_rules_ruleset_id_rulesets
        FOREIGN KEY (ruleset_id) REFERENCES inventory.rulesets (id) ON DELETE CASCADE
);
CREATE INDEX ix_ruleset_rules_ruleset_id_rule_type ON inventory.ruleset_rules (ruleset_id, rule_type);

-- Which rulesets (including inherited org/enterprise ones) apply to which repo.
CREATE TABLE inventory.repo_rulesets (
    repo_id       bigint      NOT NULL,
    ruleset_id    bigint      NOT NULL,
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_repo_rulesets PRIMARY KEY (repo_id, ruleset_id),
    CONSTRAINT fk_repo_rulesets_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id),
    CONSTRAINT fk_repo_rulesets_ruleset_id_rulesets
        FOREIGN KEY (ruleset_id) REFERENCES inventory.rulesets (id)
);
CREATE INDEX ix_repo_rulesets_ruleset_id ON inventory.repo_rulesets (ruleset_id);

-- Which classic rule and/or rulesets target each branch; exactly one FK set per row.
-- Recomputed for a repo's branches on every sync (delete + insert). Includes non-active
-- rulesets; views filter on enforcement.
CREATE TABLE inventory.branch_protection_matches (
    id                 bigint GENERATED BY DEFAULT AS IDENTITY,
    branch_id          bigint      NOT NULL,
    protection_rule_id bigint,
    ruleset_id         bigint,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_branch_protection_matches PRIMARY KEY (id),
    CONSTRAINT ck_branch_protection_matches_exactly_one_source
        CHECK (num_nonnulls(protection_rule_id, ruleset_id) = 1),
    CONSTRAINT uq_branch_protection_matches_branch_id_protection_rule_id
        UNIQUE (branch_id, protection_rule_id),
    CONSTRAINT uq_branch_protection_matches_branch_id_ruleset_id UNIQUE (branch_id, ruleset_id),
    CONSTRAINT fk_branch_protection_matches_branch_id_branches
        FOREIGN KEY (branch_id) REFERENCES inventory.branches (id) ON DELETE CASCADE,
    CONSTRAINT fk_branch_protection_matches_protection_rule_id
        FOREIGN KEY (protection_rule_id) REFERENCES inventory.branch_protection_rules (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_branch_protection_matches_ruleset_id_rulesets
        FOREIGN KEY (ruleset_id) REFERENCES inventory.rulesets (id) ON DELETE CASCADE
);
CREATE INDEX ix_branch_protection_matches_protection_rule_id
    ON inventory.branch_protection_matches (protection_rule_id);
CREATE INDEX ix_branch_protection_matches_ruleset_id
    ON inventory.branch_protection_matches (ruleset_id);

-- ---------------------------------------------------------------------------
-- Access: explicit grants. Org baseline and org owners are applied in views.
-- ---------------------------------------------------------------------------

CREATE TABLE inventory.repo_team_access (
    repo_id       bigint      NOT NULL,
    team_id       bigint      NOT NULL,
    permission    text        NOT NULL,  -- base level, ranks via permission_levels
    role_name     text        NOT NULL,  -- as shown in GitHub; may be a custom role
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_repo_team_access PRIMARY KEY (repo_id, team_id),
    CONSTRAINT fk_repo_team_access_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id),
    CONSTRAINT fk_repo_team_access_team_id_teams
        FOREIGN KEY (team_id) REFERENCES inventory.teams (id),
    CONSTRAINT fk_repo_team_access_permission_permission_levels
        FOREIGN KEY (permission) REFERENCES inventory.permission_levels (name)
);
CREATE INDEX ix_repo_team_access_team_id ON inventory.repo_team_access (team_id);

-- Direct collaborator grants only (not via team or org).
CREATE TABLE inventory.repo_user_access (
    repo_id       bigint      NOT NULL,
    user_id       bigint      NOT NULL,
    permission    text        NOT NULL,
    role_name     text        NOT NULL,
    deleted_at    timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_repo_user_access PRIMARY KEY (repo_id, user_id),
    CONSTRAINT fk_repo_user_access_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id),
    CONSTRAINT fk_repo_user_access_user_id_users
        FOREIGN KEY (user_id) REFERENCES inventory.users (id),
    CONSTRAINT fk_repo_user_access_permission_permission_levels
        FOREIGN KEY (permission) REFERENCES inventory.permission_levels (name)
);
CREATE INDEX ix_repo_user_access_user_id ON inventory.repo_user_access (user_id);

-- ---------------------------------------------------------------------------
-- CODEOWNERS (default branch)
-- ---------------------------------------------------------------------------

-- One row per repo once checked. path NULL = checked, no CODEOWNERS found.
CREATE TABLE inventory.codeowners_files (
    repo_id    bigint      NOT NULL,
    path       text,  -- .github/CODEOWNERS | CODEOWNERS | docs/CODEOWNERS
    ref        text,
    blob_sha   text,
    content    text,
    errors     jsonb       NOT NULL DEFAULT '[]',
    fetched_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_codeowners_files PRIMARY KEY (repo_id),
    CONSTRAINT fk_codeowners_files_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id)
);

-- One parsed line: path pattern -> owners (@user, @org/team, or email).
CREATE TABLE inventory.codeowners_rules (
    id          bigint GENERATED BY DEFAULT AS IDENTITY,
    repo_id     bigint  NOT NULL,
    line_number integer NOT NULL,
    pattern     text    NOT NULL,
    owners      text[]  NOT NULL,
    CONSTRAINT pk_codeowners_rules PRIMARY KEY (id),
    CONSTRAINT fk_codeowners_rules_repo_id_codeowners_files
        FOREIGN KEY (repo_id) REFERENCES inventory.codeowners_files (repo_id) ON DELETE CASCADE
);
CREATE INDEX ix_codeowners_rules_repo_id ON inventory.codeowners_rules (repo_id);

-- ---------------------------------------------------------------------------
-- GitHub Actions
-- ---------------------------------------------------------------------------

CREATE TABLE inventory.workflows (
    id                  bigint      NOT NULL,
    node_id             text        NOT NULL,
    repo_id             bigint      NOT NULL,
    name                text        NOT NULL,
    path                text        NOT NULL,
    state               text        NOT NULL,  -- active | disabled_manually | ...
    created_at          timestamptz,
    updated_at          timestamptz,
    last_run_at         timestamptz,
    last_run_status     text,
    last_run_conclusion text,
    runs_last_30d       integer,
    raw                 jsonb,
    deleted_at          timestamptz,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workflows PRIMARY KEY (id),
    CONSTRAINT uq_workflows_node_id UNIQUE (node_id),
    CONSTRAINT fk_workflows_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id)
);
CREATE INDEX ix_workflows_repo_id ON inventory.workflows (repo_id);

-- ---------------------------------------------------------------------------
-- History and operations
-- ---------------------------------------------------------------------------

-- Audit log of every collector run.
CREATE TABLE inventory.sync_runs (
    id                bigint GENERATED BY DEFAULT AS IDENTITY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text        NOT NULL DEFAULT 'running',
    mode              text        NOT NULL,
    app_version       text,
    repos_seen        integer     NOT NULL DEFAULT 0,
    repos_deep_synced integer     NOT NULL DEFAULT 0,
    branches_seen     integer     NOT NULL DEFAULT 0,
    rest_calls        integer     NOT NULL DEFAULT 0,
    graphql_points    integer     NOT NULL DEFAULT 0,
    errors            jsonb       NOT NULL DEFAULT '[]',
    CONSTRAINT pk_sync_runs PRIMARY KEY (id),
    CONSTRAINT ck_sync_runs_status CHECK (status IN ('running', 'success', 'partial', 'failed')),
    CONSTRAINT ck_sync_runs_mode CHECK (mode IN ('full', 'incremental'))
);

-- One small summary row per repo per day: trends and point-in-time questions.
CREATE TABLE inventory.repo_daily_snapshots (
    snapshot_date            date    NOT NULL,
    repo_id                  bigint  NOT NULL,
    sync_run_id              bigint,
    visibility               text    NOT NULL,
    is_archived              boolean NOT NULL,
    days_since_push          integer,
    branch_count             integer NOT NULL,
    protected_branch_count   integer NOT NULL,
    default_branch_protected boolean NOT NULL,
    default_branch_compliant boolean NOT NULL,
    workflow_count           integer NOT NULL,
    has_codeowners           boolean NOT NULL,
    team_grant_count         integer NOT NULL,
    direct_grant_count       integer NOT NULL,
    admin_user_count         integer NOT NULL,
    CONSTRAINT pk_repo_daily_snapshots PRIMARY KEY (snapshot_date, repo_id),
    CONSTRAINT fk_repo_daily_snapshots_repo_id_repositories
        FOREIGN KEY (repo_id) REFERENCES inventory.repositories (id),
    CONSTRAINT fk_repo_daily_snapshots_sync_run_id_sync_runs
        FOREIGN KEY (sync_run_id) REFERENCES inventory.sync_runs (id)
);
CREATE INDEX ix_repo_daily_snapshots_repo_id ON inventory.repo_daily_snapshots (repo_id);

-- ETag cache: GitHub doesn't count 304 Not Modified responses against the rate limit.
CREATE TABLE inventory.http_cache (
    url           text        NOT NULL,
    etag          text,
    last_modified text,
    body          jsonb       NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_http_cache PRIMARY KEY (url)
);

-- ---------------------------------------------------------------------------
-- Read-only group role for Metabase / the API. NOLOGIN: real login users are created per
-- environment and granted membership. SELECT grants on views are applied after every
-- migrate (auditor.db.views), so this role never sees the inventory tables.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auditor_read') THEN
        CREATE ROLE auditor_read NOLOGIN;
    END IF;
END $$;
