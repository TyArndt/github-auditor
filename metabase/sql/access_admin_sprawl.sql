-- Users who are admin on the most repositories (org owners excluded: admin everywhere).
SELECT login AS "User", user_name AS "Name", count(*) AS "Repos with admin"
FROM reporting.v_repo_access
WHERE effective_permission = 'admin' AND NOT is_org_owner
GROUP BY login, user_name
ORDER BY 3 DESC, login
