-- One row per CODEOWNERS rule x owner, with the owner classified.
CREATE VIEW reporting.v_codeowners AS
SELECT
    cf.repo_id,
    r.full_name AS repository,
    cf.path     AS codeowners_path,
    cr.line_number,
    cr.pattern,
    o.owner,
    CASE
        WHEN o.owner LIKE '@%/%' THEN 'team'
        WHEN o.owner LIKE '@%'   THEN 'user'
        ELSE 'email'
    END AS owner_type
FROM inventory.codeowners_files cf
JOIN inventory.repositories r ON r.id = cf.repo_id AND r.deleted_at IS NULL
JOIN inventory.codeowners_rules cr ON cr.repo_id = cf.repo_id
CROSS JOIN LATERAL unnest(cr.owners) AS o(owner);
