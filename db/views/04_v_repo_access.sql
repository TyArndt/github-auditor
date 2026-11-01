-- One row per (repo, user): the user's EFFECTIVE permission (highest across all sources)
-- and a readable list of every source that grants it.
CREATE VIEW reporting.v_repo_access AS
SELECT
    g.repo_id,
    r.full_name AS repository,
    g.user_id,
    u.login,
    u.name AS user_name,
    (array_agg(g.permission ORDER BY pl.rank DESC))[1] AS effective_permission,
    max(pl.rank) AS permission_rank,
    string_agg(
        DISTINCT g.source_type
            || coalesce(':' || g.source_name, '')
            || coalesce(' via ' || g.via_team, '')
            || ' (' || g.role_name || ')',
        ', '
    ) AS sources,
    bool_or(g.source_type = 'team')      AS has_team_grant,
    bool_or(g.source_type = 'direct')    AS has_direct_grant,
    bool_or(g.source_type = 'org_owner') AS is_org_owner
FROM reporting.v_repo_access_grants g
JOIN inventory.permission_levels pl ON pl.name = g.permission
JOIN inventory.repositories r ON r.id = g.repo_id AND r.deleted_at IS NULL
JOIN inventory.users u ON u.id = g.user_id
GROUP BY g.repo_id, r.full_name, g.user_id, u.login, u.name;
