-- Org-wide trend lines from the daily snapshots. Archived repos are excluded from the
-- compliance percentage (solution-design §5).
CREATE VIEW reporting.v_daily_trends AS
SELECT
    s.snapshot_date,
    count(*)                                         AS repo_count,
    count(*) FILTER (WHERE s.is_archived)            AS archived_count,
    count(*) FILTER (WHERE NOT s.is_archived)        AS active_repo_count,
    count(*) FILTER (WHERE NOT s.is_archived AND s.default_branch_compliant)
                                                     AS compliant_count,
    round(100.0 * count(*) FILTER (WHERE NOT s.is_archived AND s.default_branch_compliant)
          / nullif(count(*) FILTER (WHERE NOT s.is_archived), 0), 1)
                                                     AS compliance_pct,
    count(*) FILTER (WHERE s.workflow_count > 0)     AS repos_with_workflows,
    count(*) FILTER (WHERE s.has_codeowners)         AS repos_with_codeowners,
    sum(s.branch_count)                              AS branch_count,
    sum(s.protected_branch_count)                    AS protected_branch_count,
    -- NULL on days the counts weren't collected.
    sum(s.open_pull_request_count)                   AS open_pull_request_count,
    sum(s.open_dependabot_alert_count)               AS open_dependabot_alert_count,
    sum(s.open_dependabot_critical)                  AS open_dependabot_critical,
    sum(s.open_dependabot_high)                      AS open_dependabot_high
FROM inventory.repo_daily_snapshots s
GROUP BY s.snapshot_date;
