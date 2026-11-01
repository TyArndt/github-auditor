-- Active repositories whose default branch does NOT meet the standard, and why.
SELECT
    repository                          AS "Repository",
    default_branch                      AS "Default branch",
    array_to_string(failures, ', ')     AS "Fails because",
    protection_source                   AS "Protected by",
    required_reviews                    AS "Required reviews",
    blocks_force_push                   AS "Force push blocked",
    blocks_deletion                     AS "Deletion blocked",
    visibility                          AS "Visibility"
FROM reporting.v_default_branch_compliance
WHERE NOT is_compliant AND NOT is_archived
ORDER BY repository
