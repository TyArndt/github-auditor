-- How old the data is: hours since the last successful (or partial) sync finished.
SELECT round(extract(epoch FROM (now() - max(finished_at))) / 3600.0, 1) AS "Hours since last sync"
FROM reporting.v_sync_runs
WHERE status IN ('success', 'partial')
