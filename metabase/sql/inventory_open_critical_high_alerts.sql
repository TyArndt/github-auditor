-- Open critical and high severity Dependabot alerts across active repositories.
SELECT sum(open_dependabot_critical + open_dependabot_high) AS "Critical + high alerts"
FROM reporting.v_repositories
WHERE NOT is_archived
