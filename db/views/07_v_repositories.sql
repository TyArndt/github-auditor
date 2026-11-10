-- The main inventory report: one row per live repository with rolled-up facts.
CREATE VIEW reporting.v_repositories AS
WITH branch_stats AS (
    SELECT repo_id,
           count(*)                            AS branch_count,
           count(*) FILTER (WHERE is_protected) AS protected_branch_count
    FROM reporting.v_branch_protection
    GROUP BY repo_id
),
workflow_stats AS (
    SELECT repo_id,
           count(*)                                AS workflow_count,
           count(*) FILTER (WHERE state = 'active') AS active_workflow_count,
           max(last_run_at)                        AS last_workflow_run_at
    FROM inventory.workflows
    WHERE deleted_at IS NULL
    GROUP BY repo_id
),
owner_stats AS (
    SELECT repo_id,
           array_agg(DISTINCT owner ORDER BY owner) FILTER (WHERE owner_type = 'team')
               AS codeowner_teams,
           array_agg(DISTINCT owner ORDER BY owner) FILTER (WHERE owner_type = 'user')
               AS codeowner_users
    FROM reporting.v_codeowners
    GROUP BY repo_id
),
team_grants AS (
    SELECT repo_id,
           count(*) AS team_grant_count,
           array_agg(t.slug ORDER BY t.slug) FILTER (WHERE rta.permission = 'admin')
               AS admin_teams
    FROM inventory.repo_team_access rta
    JOIN inventory.teams t ON t.id = rta.team_id
    WHERE rta.deleted_at IS NULL
    GROUP BY repo_id
),
direct_grants AS (
    SELECT repo_id, count(*) AS direct_grant_count
    FROM inventory.repo_user_access
    WHERE deleted_at IS NULL
    GROUP BY repo_id
),
admin_users AS (
    SELECT repo_id, count(*) AS admin_user_count
    FROM reporting.v_repo_access
    WHERE effective_permission = 'admin' AND NOT is_org_owner
    GROUP BY repo_id
)
SELECT
    r.id AS repo_id,
    o.login AS org,
    r.name,
    r.full_name,
    r.description,
    r.visibility,
    r.is_archived,
    r.is_fork,
    r.is_template,
    r.is_empty,
    r.primary_language,
    r.topics,
    r.default_branch,
    r.created_at,
    r.updated_at,
    r.pushed_at,
    (current_date - r.pushed_at::date)        AS days_since_push,
    coalesce(bs.branch_count, 0)              AS branch_count,
    coalesce(bs.protected_branch_count, 0)    AS protected_branch_count,
    c.is_protected                            AS default_branch_protected,
    c.is_compliant                            AS default_branch_compliant,
    c.failures                                AS compliance_failures,
    coalesce(ws.workflow_count, 0)            AS workflow_count,
    coalesce(ws.active_workflow_count, 0)     AS active_workflow_count,
    (coalesce(ws.workflow_count, 0) > 0)      AS has_workflows,
    ws.last_workflow_run_at,
    cf.path                                   AS codeowners_path,
    (cf.path IS NOT NULL)                     AS has_codeowners,
    (cf.repo_id IS NOT NULL)                  AS codeowners_checked,
    os.codeowner_teams,
    os.codeowner_users,
    o.default_repository_permission           AS org_base_permission,
    coalesce(tg.team_grant_count, 0)          AS team_grant_count,
    tg.admin_teams,
    coalesce(dg.direct_grant_count, 0)        AS direct_grant_count,
    coalesce(au.admin_user_count, 0)          AS admin_user_count,
    -- NULL = not collected (App lacks the permission), not zero.
    r.open_pull_request_count,
    r.open_dependabot_alert_count,
    r.open_dependabot_critical,
    r.open_dependabot_high,
    r.open_dependabot_medium,
    r.open_dependabot_low,
    r.last_seen_at,
    r.details_synced_at
FROM inventory.repositories r
JOIN inventory.organizations o ON o.id = r.org_id
LEFT JOIN branch_stats bs                         ON bs.repo_id = r.id
LEFT JOIN reporting.v_default_branch_compliance c ON c.repo_id = r.id
LEFT JOIN workflow_stats ws                       ON ws.repo_id = r.id
LEFT JOIN inventory.codeowners_files cf           ON cf.repo_id = r.id
LEFT JOIN owner_stats os                          ON os.repo_id = r.id
LEFT JOIN team_grants tg                          ON tg.repo_id = r.id
LEFT JOIN direct_grants dg                        ON dg.repo_id = r.id
LEFT JOIN admin_users au                          ON au.repo_id = r.id
WHERE r.deleted_at IS NULL;
