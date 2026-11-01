-- Org owners (implicitly admin on every repository) and the org-wide base permission.
SELECT DISTINCT
    a.login                         AS "Org owner",
    a.user_name                     AS "Name",
    r.org_base_permission           AS "Org base permission (all members)"
FROM reporting.v_repo_access a
JOIN reporting.v_repositories r ON r.repo_id = a.repo_id
WHERE a.is_org_owner
ORDER BY a.login
