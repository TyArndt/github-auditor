-- Effective protection for every live branch, combining the classic rule (at most one per
-- branch) with all ACTIVE rulesets that target it. Rulesets in 'evaluate' or 'disabled'
-- mode do not count. When several sources apply, the most restrictive setting wins.
CREATE VIEW reporting.v_branch_protection AS
WITH classic AS (
    SELECT
        m.branch_id,
        max(CASE WHEN r.requires_approving_reviews
                 THEN r.required_approving_review_count ELSE 0 END) AS required_reviews,
        bool_or(NOT r.allows_force_pushes)       AS blocks_force_push,
        bool_or(NOT r.allows_deletions)          AS blocks_deletion,
        bool_or(r.requires_code_owner_reviews)   AS requires_code_owner_reviews,
        bool_or(r.requires_status_checks)        AS requires_status_checks,
        bool_or(r.is_admin_enforced)             AS admin_enforced,
        min(r.pattern)                           AS classic_pattern
    FROM inventory.branch_protection_matches m
    JOIN inventory.branch_protection_rules r
      ON r.id = m.protection_rule_id AND r.deleted_at IS NULL
    GROUP BY m.branch_id
),
active_rulesets AS (
    SELECT
        m.branch_id,
        array_agg(DISTINCT rs.name ORDER BY rs.name) AS ruleset_names,
        coalesce(max((rr.parameters ->> 'required_approving_review_count')::int)
                 FILTER (WHERE rr.rule_type = 'pull_request'), 0) AS required_reviews,
        coalesce(bool_or(rr.rule_type = 'non_fast_forward'), false) AS blocks_force_push,
        coalesce(bool_or(rr.rule_type = 'deletion'), false)         AS blocks_deletion,
        coalesce(bool_or(rr.rule_type = 'pull_request'
                         AND (rr.parameters ->> 'require_code_owner_review')::boolean),
                 false) AS requires_code_owner_reviews,
        coalesce(bool_or(rr.rule_type = 'required_status_checks'), false)
                                                                    AS requires_status_checks,
        bool_or(jsonb_array_length(rs.bypass_actors) > 0)           AS has_bypass_actors
    FROM inventory.branch_protection_matches m
    JOIN inventory.rulesets rs
      ON rs.id = m.ruleset_id AND rs.enforcement = 'active' AND rs.deleted_at IS NULL
    LEFT JOIN inventory.ruleset_rules rr ON rr.ruleset_id = rs.id
    GROUP BY m.branch_id
)
SELECT
    b.id                                AS branch_id,
    b.repo_id,
    r.full_name                         AS repository,
    b.name                              AS branch,
    (b.name = r.default_branch)         AS is_default,
    b.head_sha,
    b.last_commit_at,
    b.last_commit_author_login,
    (current_date - b.last_commit_at::date) AS days_since_commit,
    (c.branch_id IS NOT NULL OR s.branch_id IS NOT NULL) AS is_protected,
    CASE
        WHEN c.branch_id IS NOT NULL AND s.branch_id IS NOT NULL THEN 'both'
        WHEN c.branch_id IS NOT NULL THEN 'classic'
        WHEN s.branch_id IS NOT NULL THEN 'ruleset'
        ELSE 'none'
    END                                 AS protection_source,
    greatest(coalesce(c.required_reviews, 0), coalesce(s.required_reviews, 0))
                                        AS required_reviews,
    coalesce(c.blocks_force_push, false) OR coalesce(s.blocks_force_push, false)
                                        AS blocks_force_push,
    coalesce(c.blocks_deletion, false) OR coalesce(s.blocks_deletion, false)
                                        AS blocks_deletion,
    coalesce(c.requires_code_owner_reviews, false)
        OR coalesce(s.requires_code_owner_reviews, false)
                                        AS requires_code_owner_reviews,
    coalesce(c.requires_status_checks, false) OR coalesce(s.requires_status_checks, false)
                                        AS requires_status_checks,
    -- Informational in v1 (do not affect compliance):
    coalesce(c.admin_enforced, false)   AS classic_admin_enforced,
    coalesce(s.has_bypass_actors, false) AS ruleset_has_bypass_actors,
    c.classic_pattern,
    s.ruleset_names
FROM inventory.branches b
JOIN inventory.repositories r ON r.id = b.repo_id AND r.deleted_at IS NULL
LEFT JOIN classic c         ON c.branch_id = b.id
LEFT JOIN active_rulesets s ON s.branch_id = b.id
WHERE b.deleted_at IS NULL;
