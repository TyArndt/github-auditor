-- Every individual reason a user can access a repo (one row per user x repo x source):
--   team       : user is a member of a team granted access, or of a CHILD of that team
--                (nested teams inherit their parent's access)
--   direct     : user was added as a collaborator on the repo
--   org_owner  : org owners are implicitly admin on every repo
-- The org base permission (every member gets X) is NOT expanded here — see
-- v_repositories.org_base_permission. Expanding it would mean members x repos rows.
CREATE VIEW reporting.v_repo_access_grants AS
WITH RECURSIVE team_ancestors (team_id, ancestor_id) AS (
    -- every team is its own ancestor ...
    SELECT t.id, t.id
    FROM inventory.teams t
    WHERE t.deleted_at IS NULL
    UNION ALL
    -- ... plus its parent, grandparent, and so on up the tree
    SELECT ta.team_id, p.parent_team_id
    FROM team_ancestors ta
    JOIN inventory.teams p ON p.id = ta.ancestor_id
    WHERE p.parent_team_id IS NOT NULL
) CYCLE ancestor_id SET is_cycle USING path
SELECT
    rta.repo_id,
    tm.user_id,
    rta.permission,
    rta.role_name,
    'team'::text   AS source_type,
    granted.slug   AS source_name,
    CASE WHEN ta.team_id <> ta.ancestor_id THEN member_of.slug END AS via_team
FROM team_ancestors ta
JOIN inventory.team_members tm
  ON tm.team_id = ta.team_id AND tm.deleted_at IS NULL
JOIN inventory.repo_team_access rta
  ON rta.team_id = ta.ancestor_id AND rta.deleted_at IS NULL
JOIN inventory.teams granted   ON granted.id = ta.ancestor_id AND granted.deleted_at IS NULL
JOIN inventory.teams member_of ON member_of.id = ta.team_id
WHERE NOT ta.is_cycle

UNION ALL

SELECT rua.repo_id, rua.user_id, rua.permission, rua.role_name, 'direct', NULL, NULL
FROM inventory.repo_user_access rua
WHERE rua.deleted_at IS NULL

UNION ALL

SELECT r.id, om.user_id, 'admin', 'admin', 'org_owner', o.login, NULL
FROM inventory.repositories r
JOIN inventory.organizations o ON o.id = r.org_id
JOIN inventory.org_members om
  ON om.org_id = r.org_id AND om.role = 'admin' AND om.deleted_at IS NULL
WHERE r.deleted_at IS NULL;
