-- Open Dependabot alerts across active repositories.
-- Empty when alert counts aren't collected (needs Dependabot alerts: read).
SELECT sum(open_dependabot_alert_count) AS "Open Dependabot alerts"
FROM reporting.v_repositories
WHERE NOT is_archived
