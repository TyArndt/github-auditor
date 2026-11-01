SELECT CASE WHEN is_compliant THEN 'Compliant' ELSE 'Not compliant' END AS "Status",
       count(*) AS "Repositories"
FROM reporting.v_default_branch_compliance
WHERE NOT is_archived
GROUP BY 1
ORDER BY 1
