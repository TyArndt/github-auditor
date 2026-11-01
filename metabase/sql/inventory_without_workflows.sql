SELECT count(*) AS "Active repos without workflows"
FROM reporting.v_repositories
WHERE NOT is_archived AND NOT has_workflows
