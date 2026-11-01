-- Share of active (non-archived) repositories whose default branch meets the standard:
-- >= 1 required review, force pushes blocked, deletion blocked.
SELECT round(100.0 * count(*) FILTER (WHERE is_compliant) / nullif(count(*), 0), 1)
       AS "Default branch compliance %"
FROM reporting.v_default_branch_compliance
WHERE NOT is_archived
