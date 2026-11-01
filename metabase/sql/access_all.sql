-- Who can access each repository, at what level, and why.
-- (The org base permission applies to every member on top of this; see Org base permission.)
SELECT
    repository              AS "Repository",
    login                   AS "User",
    user_name               AS "Name",
    effective_permission    AS "Effective permission",
    sources                 AS "Granted through",
    is_org_owner            AS "Org owner"
FROM reporting.v_repo_access
ORDER BY repository, permission_rank DESC, login
