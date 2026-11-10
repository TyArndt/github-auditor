-- Open pull requests across active repositories. Empty when PR counts aren't collected
-- (needs Pull requests: read).
SELECT sum(open_pull_request_count) AS "Open pull requests"
FROM reporting.v_repositories
WHERE NOT is_archived
