-- atlas-backend CI copy (ATLAS-002 CP3, DP-3). CI USE ONLY: the job's own isolated postgres:15 service
-- container. Never run against a shared or local server: the owner runs the atlas-specs original at gate G3-R.
-- Content: a verbatim copy of atlas-specs
--   features/ATLAS-002-canonical-graph-foundation/proposals/postgres-ingest-roles-rollback.sql
-- at the pinned specs commit (specs.lock), with no CI-only block. `scripts/db/check_bootstrap.py` proves this in CI.
-- END CI HEADER
-- =============================================================================
-- ATLAS-002 — Rollback of the ingestion roles (DEC-044 amendment §10)
-- Version: v1.0 (CP3 task A2-T01). DO NOT RUN unless reverting, and only by the server
-- owner after an explicit owner decision. Touches only atlas_ingest and atlas_test_ingest.
--
--   psql -h localhost -d postgres -X -v ON_ERROR_STOP=1 -f postgres-ingest-roles-rollback.sql
--
-- Precondition, chosen by the owner:
--   (a) migration 0002 downgraded in both databases (empty databases only; it revokes its
--       grants and drops its tables), or
--   (b) the canonical tables are kept: step 1 below revokes the ingestion roles' table
--       privileges. Data is not touched, but nothing can write canonical facts afterwards.
-- DROP ROLE succeeds only when the role owns nothing and holds no privilege; by design the
-- ingestion roles own no object. No other role, database or setting is affected.
-- =============================================================================

\set ON_ERROR_STOP on

SELECT count(*) = 2 AS ingest_roles_exist FROM pg_roles WHERE rolname IN ('atlas_ingest', 'atlas_test_ingest') \gset
\if :ingest_roles_exist
\else
  DO $$ BEGIN RAISE EXCEPTION 'ABORT: the two ingestion roles do not both exist; nothing to roll back'; END $$;
\endif

-- 1. Revoke everything granted inside each database.
\connect atlas_db
BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA atlas FROM atlas_ingest;
REVOKE USAGE ON SCHEMA atlas FROM atlas_ingest;
ALTER ROLE atlas_ingest IN DATABASE atlas_db RESET search_path;
COMMIT;

\connect atlas_test
BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA atlas FROM atlas_test_ingest;
REVOKE USAGE ON SCHEMA atlas FROM atlas_test_ingest;
ALTER ROLE atlas_test_ingest IN DATABASE atlas_test RESET search_path;
COMMIT;

-- 2. Revoke CONNECT and drop the roles.
\connect postgres
BEGIN;
REVOKE CONNECT ON DATABASE atlas_db   FROM atlas_ingest;
REVOKE CONNECT ON DATABASE atlas_test FROM atlas_test_ingest;
DROP ROLE atlas_ingest, atlas_test_ingest;
COMMIT;

SELECT count(*) = 4 AS back_to_four FROM pg_roles WHERE rolname LIKE 'atlas\_%' \gset
\if :back_to_four
\else
  DO $$ BEGIN RAISE EXCEPTION 'FAIL: expected exactly the four ATLAS-001 roles after rollback'; END $$;
\endif
\echo 'Ingestion roles removed; the four ATLAS-001 roles are unchanged.'
