SELECT CASE WHEN has_workflows THEN 'Uses Actions' ELSE 'No workflows' END AS "Actions",
       count(*) AS "Repositories"
FROM reporting.v_repositories
WHERE NOT is_archived
GROUP BY 1
ORDER BY 1
