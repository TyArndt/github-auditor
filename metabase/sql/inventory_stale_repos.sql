-- Archive candidates: active repositories with no push in over 180 days.
SELECT
    full_name                               AS "Repository",
    days_since_push                         AS "Days since push",
    pushed_at                               AS "Last push",
    has_workflows                           AS "Has workflows",
    array_to_string(admin_teams, ', ')      AS "Admin teams",
    array_to_string(codeowner_teams, ', ')  AS "CODEOWNERS teams"
FROM reporting.v_repositories
WHERE NOT is_archived AND (days_since_push > 180 OR pushed_at IS NULL)
ORDER BY days_since_push DESC NULLS FIRST
