SELECT count(*) AS "Archived repositories"
FROM reporting.v_repositories
WHERE is_archived
