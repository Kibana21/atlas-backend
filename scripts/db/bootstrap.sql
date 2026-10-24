-- =============================================================================
-- atlas-backend CI bootstrap (ATLAS-001 B-22). CI USE ONLY: the job's own isolated postgres:15
-- service container. Never run against a shared or local server (use the atlas-specs proposal).
-- Content: a verbatim copy of atlas-specs
--   features/ATLAS-001-engineering-foundation/proposals/postgres-local-setup.sql
-- at the pinned specs commit bfe4a09e5d7d260afe15576b4fb9fd7530949370 (unchanged since the approved
-- v0.2.1 baseline ff3a5ae974492052b4f027290531ccf2215d9386), plus ONE block marked CI-ONLY that
-- creates atlas_db, which a fresh CI server lacks. `scripts/db/check_bootstrap.py` proves this in CI.
-- END CI HEADER
-- =============================================================================
-- ATLAS-001 — PROPOSED local PostgreSQL setup (DEC-044 / owner decision D-02)
-- STATUS: PROPOSAL FOR REVIEW — NOT APPLIED to the owner's server.
-- Version: v0.2.1 (simplified from 6 roles to 4; see spec §6.2 for the rationale).
-- Target: existing Homebrew PostgreSQL 15.12 on localhost:5432 (shared server).
-- Executor: the server owner (the local superuser), after review, using psql 15+:
--
--   export ATLAS_MIGRATOR_PASSWORD=... ATLAS_APP_PASSWORD=...           # never committed
--   export ATLAS_TEST_MIGRATOR_PASSWORD=... ATLAS_TEST_APP_PASSWORD=...
--   psql -h localhost -d postgres -X -v ON_ERROR_STOP=1 -f postgres-local-setup.sql
--
-- Scope: creates 4 Atlas login roles and `atlas_test`, and takes ownership of the
-- existing EMPTY `atlas_db`. Touches no other database, role, extension, server
-- setting or pg_hba.conf. Every step is reversible (ROLLBACK section at the end).
--
-- Role model (per environment):
--   *_migrator  owns the database and schema `atlas`; runs Alembic (DDL). No other powers.
--   *_app       runtime identity: CONNECT + USAGE on `atlas` + SELECT on tables (default).
--               Write privileges are granted per table, explicitly, by the migration that
--               creates the table (reviewed). Nothing else.
--
-- Pre-verification (read-only, 2026-10-24): atlas_db has only schema `public`,
-- 0 relations/functions/types, only plpgsql, 0 large objects, no role settings,
-- tup_inserted = 0; the only session was the owner's idle pgAdmin connection.
-- The guards below re-check this at execution time and abort if anything changed.
--
-- Dry-run evidence: see spec §6.2 (disposable postgres:15-alpine container, no host
-- port, removed afterwards; the Homebrew server was not touched).
-- =============================================================================

\set ON_ERROR_STOP on
\getenv atlas_migrator_password ATLAS_MIGRATOR_PASSWORD
\getenv atlas_app_password ATLAS_APP_PASSWORD
\getenv atlas_test_migrator_password ATLAS_TEST_MIGRATOR_PASSWORD
\getenv atlas_test_app_password ATLAS_TEST_APP_PASSWORD

-- ---------------------------------------------------------------------------
-- 0. Guards. Each failed guard raises an exception; with ON_ERROR_STOP psql exits 3
--    before any change is made.
-- ---------------------------------------------------------------------------
SELECT (current_setting('server_version_num')::int / 10000) = 15 AS is_pg15 \gset
\if :is_pg15
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: server major is not 15'; END $$;
\endif

SELECT count(*) = 0 AS no_atlas_roles FROM pg_roles WHERE rolname LIKE 'atlas\_%' \gset
\if :no_atlas_roles
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: atlas_* roles already exist — review before re-running'; END $$;
\endif

SELECT count(*) = 0 AS no_atlas_test FROM pg_database WHERE datname = 'atlas_test' \gset
\if :no_atlas_test
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: database atlas_test already exists'; END $$;
\endif

\if :{?atlas_migrator_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_MIGRATOR_PASSWORD is not set'; END $$;
\endif
\if :{?atlas_app_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_APP_PASSWORD is not set'; END $$;
\endif
\if :{?atlas_test_migrator_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_TEST_MIGRATOR_PASSWORD is not set'; END $$;
\endif
\if :{?atlas_test_app_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: ATLAS_TEST_APP_PASSWORD is not set'; END $$;
\endif
SELECT :'atlas_migrator_password' <> '' AND :'atlas_app_password' <> ''
   AND :'atlas_test_migrator_password' <> '' AND :'atlas_test_app_password' <> '' AS have_passwords \gset
\if :have_passwords
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: one or more ATLAS_*_PASSWORD environment variables are empty'; END $$;
\endif

-- BEGIN CI-ONLY: the only difference from the proposal. A fresh CI server has no atlas_db;
-- create it empty (same locale as the local server) so the proposal's guards and steps apply as-is.
CREATE DATABASE atlas_db TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';
-- END CI-ONLY
\connect atlas_db
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')) = 0
  AND (SELECT count(*) FROM pg_namespace
     WHERE nspname NOT IN ('pg_catalog','information_schema','pg_toast','public')
       AND nspname NOT LIKE 'pg\_temp%' AND nspname NOT LIKE 'pg\_toast\_temp%') = 0
  AND (SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql') = 0
  AND (SELECT count(*) FROM pg_largeobject_metadata) = 0 AS atlas_db_empty \gset
\if :atlas_db_empty
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: atlas_db is no longer empty — do not reuse without review'; END $$;
\endif
\connect postgres

-- ---------------------------------------------------------------------------
-- 1. Roles. No SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS (defaults).
-- ---------------------------------------------------------------------------
BEGIN;
CREATE ROLE atlas_migrator      LOGIN CONNECTION LIMIT 3  PASSWORD :'atlas_migrator_password';
CREATE ROLE atlas_app           LOGIN CONNECTION LIMIT 10 PASSWORD :'atlas_app_password';
CREATE ROLE atlas_test_migrator LOGIN CONNECTION LIMIT 3  PASSWORD :'atlas_test_migrator_password';
CREATE ROLE atlas_test_app      LOGIN CONNECTION LIMIT 10 PASSWORD :'atlas_test_app_password';

-- Protect the shared server from runaway Atlas sessions.
ALTER ROLE atlas_app           SET statement_timeout = '30s';
ALTER ROLE atlas_app           SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE atlas_test_app      SET statement_timeout = '30s';
ALTER ROLE atlas_test_app      SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE atlas_migrator      SET lock_timeout = '10s';
ALTER ROLE atlas_test_migrator SET lock_timeout = '10s';

-- 2. Local database: reuse the verified-empty atlas_db.
ALTER DATABASE atlas_db OWNER TO atlas_migrator;
REVOKE ALL ON DATABASE atlas_db FROM PUBLIC;                -- removes default CONNECT/TEMP
GRANT CONNECT ON DATABASE atlas_db TO atlas_app;
COMMIT;

-- 3. Test database (CREATE DATABASE cannot run inside a transaction block).
--    Locale C/UTF8 matches atlas_db and the server default (DEC-083 Open).
CREATE DATABASE atlas_test OWNER atlas_test_migrator TEMPLATE template0
  ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';

BEGIN;
REVOKE ALL ON DATABASE atlas_test FROM PUBLIC;
GRANT CONNECT ON DATABASE atlas_test TO atlas_test_app;
COMMIT;

-- ---------------------------------------------------------------------------
-- 4. Schema and default privileges inside atlas_db
-- ---------------------------------------------------------------------------
\connect atlas_db
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;                     -- Atlas uses schema `atlas` only
CREATE SCHEMA atlas AUTHORIZATION atlas_migrator;
REVOKE ALL ON SCHEMA atlas FROM PUBLIC;
GRANT USAGE ON SCHEMA atlas TO atlas_app;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas
  GRANT SELECT ON TABLES TO atlas_app;                        -- read-only by default
ALTER ROLE atlas_app      IN DATABASE atlas_db SET search_path = atlas;
ALTER ROLE atlas_migrator IN DATABASE atlas_db SET search_path = atlas;
COMMIT;

-- 5. Same pattern inside atlas_test
\connect atlas_test
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA atlas AUTHORIZATION atlas_test_migrator;
REVOKE ALL ON SCHEMA atlas FROM PUBLIC;
GRANT USAGE ON SCHEMA atlas TO atlas_test_app;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_test_migrator IN SCHEMA atlas
  GRANT SELECT ON TABLES TO atlas_test_app;
ALTER ROLE atlas_test_app      IN DATABASE atlas_test SET search_path = atlas;
ALTER ROLE atlas_test_migrator IN DATABASE atlas_test SET search_path = atlas;
COMMIT;

\connect postgres
\echo 'Atlas local PostgreSQL setup complete. Run the VERIFY queries below (or `make doctor`).'

-- =============================================================================
-- VERIFY (read-only; expected results in comments)
-- =============================================================================
-- SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit
--   FROM pg_roles WHERE rolname LIKE 'atlas\_%' ORDER BY 1;
--   -> 4 login roles; all flags false; limits 3/10
-- SELECT datname, pg_get_userbyid(datdba), datacl FROM pg_database WHERE datname IN ('atlas_db','atlas_test');
--   -> owners atlas_migrator / atlas_test_migrator; no PUBLIC ('=') entry
-- \connect atlas_db
-- SELECT has_schema_privilege('atlas_app','atlas','CREATE');             -> false
-- SELECT has_database_privilege('atlas_app','atlas_db','TEMP');          -> false
-- SELECT has_database_privilege('atlas_app','atlas_test','CONNECT');     -> false
-- SELECT has_database_privilege('atlas_test_app','atlas_db','CONNECT');  -> false
-- SELECT has_database_privilege('atlas_migrator','atlas_test','CONNECT');-> false

-- =============================================================================
-- ROLLBACK (do not run unless reverting). Restores atlas_db to its verified prior
-- state (owner = the original owner recorded before applying; PUBLIC CONNECT/TEMP;
-- schema public USAGE for PUBLIC). Replace <original-owner> below before running.
-- Only safe while Atlas has created no data you want to keep.
-- =============================================================================
-- \connect postgres
-- DROP DATABASE IF EXISTS atlas_test;
-- \connect atlas_db
-- DROP SCHEMA IF EXISTS atlas CASCADE;
-- GRANT USAGE ON SCHEMA public TO PUBLIC;
-- ALTER ROLE atlas_app      IN DATABASE atlas_db RESET search_path;
-- ALTER ROLE atlas_migrator IN DATABASE atlas_db RESET search_path;
-- \connect postgres
-- ALTER DATABASE atlas_db OWNER TO "<original-owner>";
-- GRANT CONNECT, TEMPORARY ON DATABASE atlas_db TO PUBLIC;
-- REVOKE CONNECT ON DATABASE atlas_db FROM atlas_app;
-- DROP ROLE atlas_app, atlas_migrator, atlas_test_app, atlas_test_migrator;

-- =============================================================================
-- NOTES
-- * Local authentication: pg_hba.conf uses `trust` for local/loopback connections, so
--   these passwords are NOT verified locally (DEC-084). Role separation still limits
--   what each identity may do. Optional Atlas-only hardening: proposals/pg_hba-atlas.conf.
-- * CI: isolated postgres:15 service with scram-sha-256 for all connections; the
--   backend's scripts/db/bootstrap.sql (created only after approval) differs only by
--   also creating atlas_db, which does not exist in a fresh CI container.
-- =============================================================================
