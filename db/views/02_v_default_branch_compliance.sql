-- One row per live repository: does its default branch meet the v1 minimum standard?
--   >= 1 required approving review, force pushes blocked, deletion blocked
-- (classic rule or ACTIVE ruleset). Change the standard here and only here.
CREATE VIEW reporting.v_default_branch_compliance AS
WITH d AS (
    SELECT
        r.id AS repo_id,
        r.full_name AS repository,
        r.default_branch,
        r.visibility,
        r.is_archived,
        r.is_empty,
        bp.branch_id IS NOT NULL               AS default_branch_found,
        coalesce(bp.is_protected, false)       AS is_protected,
        coalesce(bp.protection_source, 'none') AS protection_source,
        coalesce(bp.required_reviews, 0)       AS required_reviews,
        coalesce(bp.blocks_force_push, false)  AS blocks_force_push,
        coalesce(bp.blocks_deletion, false)    AS blocks_deletion,
        coalesce(bp.requires_code_owner_reviews, false) AS requires_code_owner_reviews,
        coalesce(bp.classic_admin_enforced, false)      AS classic_admin_enforced,
        coalesce(bp.ruleset_has_bypass_actors, false)   AS ruleset_has_bypass_actors
    FROM inventory.repositories r
    LEFT JOIN reporting.v_branch_protection bp ON bp.repo_id = r.id AND bp.is_default
    WHERE r.deleted_at IS NULL
)
SELECT
    d.*,
    (d.required_reviews >= 1 AND d.blocks_force_push AND d.blocks_deletion) AS is_compliant,
    array_remove(ARRAY[
        CASE WHEN NOT d.default_branch_found THEN 'default branch not found' END,
        CASE WHEN d.required_reviews < 1     THEN 'no required review' END,
        CASE WHEN NOT d.blocks_force_push    THEN 'force push allowed' END,
        CASE WHEN NOT d.blocks_deletion      THEN 'deletion allowed' END
    ], NULL) AS failures
FROM d;
