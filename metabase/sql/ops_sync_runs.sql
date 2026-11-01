-- Collector health: most recent runs first.
SELECT
    sync_run_id         AS "Run",
    started_at          AS "Started",
    duration_minutes    AS "Minutes",
    status              AS "Status",
    repos_seen          AS "Repos seen",
    repos_deep_synced   AS "Repos detailed",
    branches_seen       AS "Branches",
    rest_calls          AS "REST calls",
    graphql_points      AS "GraphQL points",
    error_count         AS "Errors"
FROM reporting.v_sync_runs
ORDER BY started_at DESC
LIMIT 50
