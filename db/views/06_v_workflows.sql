-- Live GitHub Actions workflows with their repository.
CREATE VIEW reporting.v_workflows AS
SELECT
    w.id AS workflow_id,
    w.repo_id,
    r.full_name AS repository,
    w.name,
    w.path,
    w.state,
    w.created_at,
    w.updated_at,
    w.last_run_at,
    (current_date - w.last_run_at::date) AS days_since_last_run,
    w.last_run_status,
    w.last_run_conclusion,
    w.runs_last_30d,
    (w.last_run_at IS NULL) AS never_run
FROM inventory.workflows w
JOIN inventory.repositories r ON r.id = w.repo_id AND r.deleted_at IS NULL
WHERE w.deleted_at IS NULL;
