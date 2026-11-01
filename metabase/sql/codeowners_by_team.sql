-- Teams and users named in CODEOWNERS, by number of repositories.
SELECT owner AS "Owner", owner_type AS "Type", count(DISTINCT repo_id) AS "Repositories"
FROM reporting.v_codeowners
GROUP BY owner, owner_type
ORDER BY 3 DESC, owner
