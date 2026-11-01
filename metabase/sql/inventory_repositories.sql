-- Every live repository with its key facts.
SELECT
    full_name                                   AS "Repository",
    visibility                                  AS "Visibility",
    is_archived                                 AS "Archived",
    primary_language                            AS "Language",
    default_branch                              AS "Default branch",
    pushed_at                                   AS "Last push",
    days_since_push                             AS "Days since push",
    branch_count                                AS "Branches",
    protected_branch_count                      AS "Protected branches",
    default_branch_compliant                    AS "Default branch compliant",
    workflow_count                              AS "Workflows",
    last_workflow_run_at                        AS "Last workflow run",
    array_to_string(codeowner_teams, ', ')      AS "CODEOWNERS teams",
    array_to_string(admin_teams, ', ')          AS "Admin teams",
    team_grant_count                            AS "Team grants",
    direct_grant_count                          AS "Direct grants",
    admin_user_count                            AS "Admin users"
FROM reporting.v_repositories
ORDER BY full_name
