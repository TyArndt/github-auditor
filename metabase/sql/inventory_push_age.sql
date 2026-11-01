-- Active repositories by time since last push.
SELECT bucket AS "Last push", count(*) AS "Repositories"
FROM (
    SELECT CASE
               WHEN days_since_push IS NULL THEN '6. never'
               WHEN days_since_push <= 30   THEN '1. 0-30 days'
               WHEN days_since_push <= 90   THEN '2. 31-90 days'
               WHEN days_since_push <= 180  THEN '3. 91-180 days'
               WHEN days_since_push <= 365  THEN '4. 181-365 days'
               ELSE '5. over 1 year'
           END AS bucket
    FROM reporting.v_repositories
    WHERE NOT is_archived
) t
GROUP BY bucket
ORDER BY bucket
