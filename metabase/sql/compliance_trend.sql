-- One point per daily snapshot (needs a few days of syncs to show a line).
SELECT snapshot_date AS "Date", compliance_pct AS "Compliance %"
FROM reporting.v_daily_trends
ORDER BY snapshot_date
