-- Why repositories fail (a repo can fail for several reasons).
SELECT failure AS "Reason", count(*) AS "Repositories"
FROM reporting.v_default_branch_compliance,
     unnest(failures) AS failure
WHERE NOT is_archived
GROUP BY failure
ORDER BY 2 DESC
