-- Collector health: every run with its duration and error count.
CREATE VIEW reporting.v_sync_runs AS
SELECT
    id AS sync_run_id,
    started_at,
    finished_at,
    round(extract(epoch FROM (finished_at - started_at)) / 60.0, 1) AS duration_minutes,
    status,
    mode,
    app_version,
    repos_seen,
    repos_deep_synced,
    branches_seen,
    rest_calls,
    graphql_points,
    jsonb_array_length(errors) AS error_count,
    errors
FROM inventory.sync_runs;
