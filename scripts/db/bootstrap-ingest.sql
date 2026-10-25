-- atlas-backend CI copy (ATLAS-002 CP3, DP-3). CI USE ONLY: the job's own isolated postgres:15 service
-- container. Never run against a shared or local server: the owner runs the atlas-specs original at gate G3-R.
-- Content: a verbatim copy of atlas-specs
--   features/ATLAS-002-canonical-graph-foundation/proposals/postgres-ingest-roles.sql
-- at the pinned specs commit (specs.lock), with no CI-only block. `scripts/db/check_bootstrap.py` proves this in CI.
-- END CI HEADER
-- =============================================================================
-- ATLAS-002 — Ingestion roles for the local PostgreSQL server (DEC-044 as amended, D17)
-- STATUS: PREPARED FOR GATE G3-R. NOT APPLIED. Run only by the server owner, after review
-- and the owner's explicit G3-R authorization. Claude never runs this script.
-- Version: v1.0 (CP3 task A2-T01), from proposals/dec-044-amendment.md §7.
-- Target: the existing PostgreSQL 15 server that already holds the ATLAS-001 setup
-- (postgres-local-setup.sql v0.2.1). That script is NOT re-run; this one is additive.
-- Executor: the server owner (the local superuser), using psql 15+:
--
--   export ATLAS_INGEST_PASSWORD=... ATLAS_TEST_INGEST_PASSWORD=...     # owner-created; never committed
--   psql -h localhost -d postgres -X -v ON_ERROR_STOP=1 -f postgres-ingest-roles.sql
--
-- Scope: creates exactly two login roles, atlas_ingest (atlas_db) and atlas_test_ingest
-- (atlas_test), with CONNECT on their own database, USAGE on schema `atlas` there, session
-- limits and search_path. It grants NO table privilege (migration 0002 does, per table),
-- and touches no other role, database, schema, extension, server setting or pg_hba.conf.
-- Verification: postgres-ingest-roles-verify.sql (read-only). Rollback:
-- postgres-ingest-roles-rollback.sql. Owner steps: g3r-runbook.md.
-- =============================================================================

\set ON_ERROR_STOP on
\getenv atlas_ingest_password ATLAS_INGEST_PASSWORD
\getenv atlas_test_ingest_password ATLAS_TEST_INGEST_PASSWORD

-- ---------------------------------------------------------------------------
-- 0. Guards. Each failed guard raises an exception; with ON_ERROR_STOP psql exits 3
--    before any change is made.
-- ---------------------------------------------------------------------------
SELECT (current_setting('server_version_num')::int / 10000) = 15 AS is_pg15 \gset
\if :is_pg15
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: server major is not 15'; END $$;
\endif

SELECT count(*) = 4 AS atlas001_roles_ok FROM pg_roles
 WHERE rolname IN ('atlas_migrator', 'atlas_app', 'atlas_test_migrator', 'atlas_test_app')
   AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
   AND NOT rolreplication AND NOT rolbypassrls \gset
\if :atlas001_roles_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: the four ATLAS-001 roles are missing or have special attributes'; END $$;
\endif

SELECT count(*) = 4 AS only_atlas001_roles FROM pg_roles WHERE rolname LIKE 'atlas\_%' \gset
\if :only_atlas001_roles
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: atlas_* roles other than the four ATLAS-001 roles exist (ingest roles already created?)'; END $$;
\endif

SELECT count(*) = 2 AS databases_ok FROM pg_database
 WHERE (datname = 'atlas_db' AND pg_get_userbyid(datdba) = 'atlas_migrator')
    OR (datname = 'atlas_test' AND pg_get_userbyid(datdba) = 'atlas_test_migrator') \gset
\if :databases_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: atlas_db / atlas_test missing or not owned by their migrator roles'; END $$;
\endif

SELECT NOT has_database_privilege('public', 'atlas_db', 'CONNECT')
   AND NOT has_database_privilege('public', 'atlas_db', 'TEMP')
   AND NOT has_database_privilege('public', 'atlas_test', 'CONNECT')
   AND NOT has_database_privilege('public', 'atlas_test', 'TEMP') AS public_closed \gset
\if :public_closed
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: PUBLIC has CONNECT or TEMP on atlas_db/atlas_test (ATLAS-001 state changed)'; END $$;
\endif

\if :{?atlas_ingest_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_INGEST_PASSWORD is not set'; END $$;
\endif
\if :{?atlas_test_ingest_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_TEST_INGEST_PASSWORD is not set'; END $$;
\endif
SELECT :'atlas_ingest_password' <> '' AND :'atlas_test_ingest_password' <> ''
   AND :'atlas_ingest_password' <> :'atlas_test_ingest_password' AS have_passwords \gset
\if :have_passwords
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ingest passwords must be set, non-empty and different'; END $$;
\endif

\connect atlas_db
SELECT count(*) = 1 AS atlas_schema_ok FROM pg_namespace
 WHERE nspname = 'atlas' AND pg_get_userbyid(nspowner) = 'atlas_migrator' \gset
\if :atlas_schema_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: schema atlas missing in atlas_db or not owned by atlas_migrator'; END $$;
\endif
\connect atlas_test
SELECT count(*) = 1 AS atlas_test_schema_ok FROM pg_namespace
 WHERE nspname = 'atlas' AND pg_get_userbyid(nspowner) = 'atlas_test_migrator' \gset
\if :atlas_test_schema_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: schema atlas missing in atlas_test or not owned by atlas_test_migrator'; END $$;
\endif
\connect postgres

-- ---------------------------------------------------------------------------
-- 1. Roles. No SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS (defaults);
--    no membership in any role; no default privileges.
-- ---------------------------------------------------------------------------
BEGIN;
CREATE ROLE atlas_ingest      LOGIN CONNECTION LIMIT 2 PASSWORD :'atlas_ingest_password';
CREATE ROLE atlas_test_ingest LOGIN CONNECTION LIMIT 2 PASSWORD :'atlas_test_ingest_password';
ALTER ROLE atlas_ingest      SET statement_timeout = '120s';
ALTER ROLE atlas_ingest      SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE atlas_ingest      SET lock_timeout = '10s';
ALTER ROLE atlas_test_ingest SET statement_timeout = '120s';
ALTER ROLE atlas_test_ingest SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE atlas_test_ingest SET lock_timeout = '10s';
GRANT CONNECT ON DATABASE atlas_db   TO atlas_ingest;
GRANT CONNECT ON DATABASE atlas_test TO atlas_test_ingest;
COMMIT;

-- ---------------------------------------------------------------------------
-- 2. Schema usage and search_path, per database. No table privilege here.
-- ---------------------------------------------------------------------------
\connect atlas_db
BEGIN;
GRANT USAGE ON SCHEMA atlas TO atlas_ingest;
ALTER ROLE atlas_ingest IN DATABASE atlas_db SET search_path = atlas;
COMMIT;

\connect atlas_test
BEGIN;
GRANT USAGE ON SCHEMA atlas TO atlas_test_ingest;
ALTER ROLE atlas_test_ingest IN DATABASE atlas_test SET search_path = atlas;
COMMIT;

\connect postgres
\echo 'Atlas ingestion roles created. Run postgres-ingest-roles-verify.sql (read-only) next.'

-- =============================================================================
-- NOTES
-- * Local authentication: the server uses `trust` for local/loopback connections
--   (DEC-084 Open, E-07 open), so these passwords are not verified locally. Role
--   separation still limits what each identity may do. CI uses scram-sha-256.
-- * CI: atlas-backend scripts/db/bootstrap-ingest.sql is a verbatim copy of this file
--   (plus a CI header), drift-checked by scripts/db/check_bootstrap.py.
-- =============================================================================
