-- LOCAL DEVELOPMENT ONLY (run by the `db-setup` service in docker-compose.yml).
-- Idempotent: safe to run on every `docker compose --profile reports up`.
--
--   metabase         owns Metabase's own application database (users, questions, dashboards)
--   metabase_reader  the login Metabase uses to READ our data; member of auditor_read, which
--                    can SELECT the `reporting` views only (never the `inventory` tables)
--
-- In AWS (Phase 4) these roles get generated passwords from Secrets Manager instead.

SELECT 'CREATE ROLE metabase LOGIN PASSWORD ''metabase'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase')\gexec

SELECT 'CREATE DATABASE metabase OWNER metabase'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'metabase')\gexec

-- Normally created by migration 0001; created here too so the order of setup doesn't matter.
SELECT 'CREATE ROLE auditor_read NOLOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'auditor_read')\gexec

SELECT 'CREATE ROLE metabase_reader LOGIN PASSWORD ''metabase_reader'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_reader')\gexec

GRANT auditor_read TO metabase_reader;
