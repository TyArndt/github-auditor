SELECT count(*) AS "Active repos without CODEOWNERS"
FROM reporting.v_repositories
WHERE NOT is_archived AND codeowners_checked AND NOT has_codeowners
