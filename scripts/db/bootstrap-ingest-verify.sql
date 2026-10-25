-- atlas-backend CI copy (ATLAS-002 CP3, DP-3). CI USE ONLY: the job's own isolated postgres:15 service
-- container. Never run against a shared or local server: the owner runs the atlas-specs original at gate G3-R.
-- Content: a verbatim copy of atlas-specs
--   features/ATLAS-002-canonical-graph-foundation/proposals/postgres-ingest-roles-verify.sql
-- at the pinned specs commit (specs.lock), with no CI-only block. `scripts/db/check_bootstrap.py` proves this in CI.
-- END CI HEADER
-- =============================================================================
-- ATLAS-002 — Read-only verification of the ingestion roles (Gate G3-R)
-- Version: v1.0 (CP3 task A2-T01). Makes no change: only catalog SELECTs and \connect.
-- Run by the server owner after postgres-ingest-roles.sql, as the same superuser:
--
--   psql -h localhost -d postgres -X -v ON_ERROR_STOP=1 -f postgres-ingest-roles-verify.sql
--
-- Before migration 0002 it also asserts that the ingestion roles have no table
-- privilege at all. After migration 0002 add `-v after_0002=1`: the per-table grants
-- are then checked by the backend's privilege tests and `atlas doctor`.
-- Each failed check raises an exception, so psql stops with exit code 3.
-- =============================================================================

\set ON_ERROR_STOP on

-- 1. Six Atlas login roles, no special attributes, expected connection limits.
SELECT count(*) = 6 AS six_roles FROM pg_roles r
 WHERE r.rolname LIKE 'atlas\_%' AND r.rolcanlogin
   AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
   AND r.rolconnlimit = CASE WHEN r.rolname LIKE '%migrator' THEN 3 WHEN r.rolname LIKE '%ingest' THEN 2 ELSE 10 END \gset
\if :six_roles
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: expected six atlas_* login roles without special attributes and limits 3/10/2'; END $$;
\endif

-- 2. The ingestion roles belong to no role, and no role belongs to them.
SELECT count(*) = 0 AS no_membership FROM pg_auth_members m
 WHERE m.member IN (SELECT oid FROM pg_roles WHERE rolname IN ('atlas_ingest', 'atlas_test_ingest'))
    OR m.roleid IN (SELECT oid FROM pg_roles WHERE rolname IN ('atlas_ingest', 'atlas_test_ingest')) \gset
\if :no_membership
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: an ingestion role has a role membership'; END $$;
\endif

-- 3. Session limits (role-level settings).
SELECT count(*) = 2 AS limits_ok FROM pg_roles
 WHERE rolname IN ('atlas_ingest', 'atlas_test_ingest')
   AND rolconfig @> ARRAY['statement_timeout=120s', 'idle_in_transaction_session_timeout=60s', 'lock_timeout=10s'] \gset
\if :limits_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: ingestion role session limits differ from 120s / 60s / 10s'; END $$;
\endif

-- 4. Each ingestion role connects to its own database only, and nowhere has TEMP.
SELECT has_database_privilege('atlas_ingest', 'atlas_db', 'CONNECT')
   AND has_database_privilege('atlas_test_ingest', 'atlas_test', 'CONNECT')
   AND NOT has_database_privilege('atlas_ingest', 'atlas_test', 'CONNECT')
   AND NOT has_database_privilege('atlas_test_ingest', 'atlas_db', 'CONNECT')
   AND NOT has_database_privilege('atlas_ingest', 'atlas_db', 'TEMP')
   AND NOT has_database_privilege('atlas_test_ingest', 'atlas_test', 'TEMP')
   AND NOT has_database_privilege('atlas_ingest', 'atlas_db', 'CREATE')
   AND NOT has_database_privilege('atlas_test_ingest', 'atlas_test', 'CREATE') AS database_privileges_ok \gset
\if :database_privileges_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: ingestion database privileges (CONNECT own only, no TEMP, no CREATE)'; END $$;
\endif

-- 5. Inside each database: USAGE without CREATE on schema atlas; search_path = atlas;
--    before migration 0002, no table, sequence or function privilege.
\connect atlas_db
SELECT has_schema_privilege('atlas_ingest', 'atlas', 'USAGE')
   AND NOT has_schema_privilege('atlas_ingest', 'atlas', 'CREATE')
   AND NOT has_schema_privilege('atlas_ingest', 'public', 'CREATE') AS schema_ok \gset
\if :schema_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_ingest schema privileges in atlas_db'; END $$;
\endif
SELECT count(*) = 1 AS search_path_ok FROM pg_db_role_setting s
 WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = 'atlas_db')
   AND s.setrole = (SELECT oid FROM pg_roles WHERE rolname = 'atlas_ingest')
   AND s.setconfig = ARRAY['search_path=atlas'] \gset
\if :search_path_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_ingest search_path in atlas_db is not atlas'; END $$;
\endif
\if :{?after_0002}
\else
  SELECT count(*) = 0 AS no_table_grants FROM information_schema.role_table_grants WHERE grantee = 'atlas_ingest' \gset
  \if :no_table_grants
  \else
    DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_ingest already has table privileges before migration 0002'; END $$;
  \endif
\endif

\connect atlas_test
SELECT has_schema_privilege('atlas_test_ingest', 'atlas', 'USAGE')
   AND NOT has_schema_privilege('atlas_test_ingest', 'atlas', 'CREATE')
   AND NOT has_schema_privilege('atlas_test_ingest', 'public', 'CREATE') AS test_schema_ok \gset
\if :test_schema_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_test_ingest schema privileges in atlas_test'; END $$;
\endif
SELECT count(*) = 1 AS test_search_path_ok FROM pg_db_role_setting s
 WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = 'atlas_test')
   AND s.setrole = (SELECT oid FROM pg_roles WHERE rolname = 'atlas_test_ingest')
   AND s.setconfig = ARRAY['search_path=atlas'] \gset
\if :test_search_path_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_test_ingest search_path in atlas_test is not atlas'; END $$;
\endif
\if :{?after_0002}
\else
  SELECT count(*) = 0 AS no_test_table_grants FROM information_schema.role_table_grants WHERE grantee = 'atlas_test_ingest' \gset
  \if :no_test_table_grants
  \else
    DO $$ BEGIN RAISE EXCEPTION 'FAIL: atlas_test_ingest already has table privileges before migration 0002'; END $$;
  \endif
\endif

\connect postgres
\echo 'OK: ingestion roles verified (read-only).'
