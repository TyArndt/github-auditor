-- Access granted to individuals directly instead of through a team (harder to review).
SELECT
    repository              AS "Repository",
    login                   AS "User",
    effective_permission    AS "Effective permission",
    sources                 AS "Granted through"
FROM reporting.v_repo_access
WHERE has_direct_grant
ORDER BY permission_rank DESC, repository, login
