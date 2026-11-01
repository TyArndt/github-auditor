-- Non-default branches with no commit in over 90 days (cleanup candidates).
SELECT
    repository                  AS "Repository",
    branch                      AS "Branch",
    days_since_commit           AS "Days since commit",
    last_commit_author_login    AS "Last commit by",
    is_protected                AS "Protected"
FROM reporting.v_branch_protection
WHERE NOT is_default AND days_since_commit > 90
ORDER BY days_since_commit DESC
