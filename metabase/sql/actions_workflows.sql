-- Every GitHub Actions workflow and its most recent run.
SELECT
    repository              AS "Repository",
    name                    AS "Workflow",
    path                    AS "File",
    state                   AS "State",
    last_run_at             AS "Last run",
    days_since_last_run     AS "Days since last run",
    last_run_conclusion     AS "Last result",
    runs_last_30d           AS "Runs (30 days)",
    never_run               AS "Never run"
FROM reporting.v_workflows
ORDER BY repository, name
