-- Active repositories with no CODEOWNERS file on the default branch.
SELECT
    full_name                           AS "Repository",
    array_to_string(admin_teams, ', ')  AS "Admin teams (likely owners)",
    days_since_push                     AS "Days since push"
FROM reporting.v_repositories
WHERE NOT is_archived AND codeowners_checked AND NOT has_codeowners
ORDER BY full_name
