-- Active repositories with open Dependabot alerts, worst first. Blank = alert counts not
-- collected (the GitHub App needs Dependabot alerts: read).
SELECT
    full_name                               AS "Repository",
    open_dependabot_critical                AS "Critical",
    open_dependabot_high                    AS "High",
    open_dependabot_medium                  AS "Medium",
    open_dependabot_low                     AS "Low",
    open_dependabot_alert_count             AS "Total open alerts",
    open_pull_request_count                 AS "Open PRs",
    array_to_string(admin_teams, ', ')      AS "Admin teams",
    array_to_string(codeowner_teams, ', ')  AS "CODEOWNERS teams"
FROM reporting.v_repositories
WHERE NOT is_archived AND open_dependabot_alert_count > 0
ORDER BY open_dependabot_critical DESC, open_dependabot_high DESC,
         open_dependabot_alert_count DESC, full_name
