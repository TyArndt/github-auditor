-- Every live branch and what protects it.
SELECT
    repository                          AS "Repository",
    branch                              AS "Branch",
    is_default                          AS "Default",
    is_protected                        AS "Protected",
    protection_source                   AS "Protected by",
    required_reviews                    AS "Required reviews",
    blocks_force_push                   AS "Force push blocked",
    blocks_deletion                     AS "Deletion blocked",
    requires_status_checks              AS "Status checks",
    classic_pattern                     AS "Classic rule pattern",
    array_to_string(ruleset_names, ', ') AS "Rulesets",
    days_since_commit                   AS "Days since commit",
    last_commit_author_login            AS "Last commit by"
FROM reporting.v_branch_protection
ORDER BY repository, is_default DESC, branch
