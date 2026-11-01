SELECT count(*) AS "Active repositories"
FROM reporting.v_repositories
WHERE NOT is_archived
