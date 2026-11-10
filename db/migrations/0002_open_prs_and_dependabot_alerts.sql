-- 0002 — Open pull request and open Dependabot alert counts per repository.
-- Refreshed on every sync from org-wide listings (not the per-repo detail pass).
-- NULL = not collected (the App lacks the permission, or the read failed). Reports must
-- keep that distinct from 0.

ALTER TABLE inventory.repositories
    ADD COLUMN open_pull_request_count      integer,
    ADD COLUMN open_dependabot_alert_count  integer,
    ADD COLUMN open_dependabot_critical     integer,
    ADD COLUMN open_dependabot_high         integer,
    ADD COLUMN open_dependabot_medium       integer,
    ADD COLUMN open_dependabot_low          integer;

ALTER TABLE inventory.repo_daily_snapshots
    ADD COLUMN open_pull_request_count      integer,
    ADD COLUMN open_dependabot_alert_count  integer,
    ADD COLUMN open_dependabot_critical     integer,
    ADD COLUMN open_dependabot_high         integer;
