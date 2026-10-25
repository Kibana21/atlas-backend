"""Migration 0002 on real PostgreSQL 15: seed, privileges, triggers and attribution (AC-CP3-02/03/04/07).

Every write runs inside ``transaction(force_rollback=True)``: nothing a test writes survives it.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors, sql
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.domain.canonical import taxonomy
from atlas.main import create_app
from atlas.persistence.canonical import schema_0002
from atlas.persistence.migrations import metadata
from tests.integration.conftest import connect, tx_connect

pytestmark = [pytest.mark.integration, pytest.mark.security]
CANONICAL = sorted(t.name for t in metadata.sorted_tables)
SERVICE = ("service", "atlas.service", "fixture-loader")


def default_org(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT id::text FROM atlas.organization WHERE key = 'default'").fetchone()
    assert row is not None
    return str(row[0])


def insert_change_set(conn: psycopg.Connection[Any], org: str, revision: int = 1, **overrides: Any) -> None:
    values = {
        "org_id": org,
        "revision": revision,
        "kind": "fixture_load",
        "channel": "fixture_cli",
        "actor_kind": SERVICE[0],
        "actor_namespace": SERVICE[1],
        "actor_key": SERVICE[2],
        "source_ref": "test",
        "reason": "integration test",
        "created_at": None,
        "db_role": None,
        **overrides,
    }
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in values)
    conn.execute(
        sql.SQL("INSERT INTO atlas.change_set ({}) VALUES ({})").format(
            cols, sql.SQL(", ").join([sql.Placeholder()] * len(values))
        ),
        list(values.values()),
    )


# --------------------------------------------------------------------------- AC-CP3-07: seed equals the artifact


def test_reference_tables_equal_the_packaged_taxonomy(migrated: None, settings: Settings) -> None:
    seed = taxonomy.seed_rows(taxonomy.packaged("1.1.0"))
    with connect(settings.database_url) as conn:
        for table, expected in seed.items():
            columns = list(expected[0])
            cur = conn.cursor(row_factory=psycopg.rows.dict_row)
            rows = cur.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier("atlas", table)
                )
            ).fetchall()

            def key(r: dict[str, Any], columns: list[str] = columns) -> list[str]:
                return [str(r[c]) for c in columns]

            assert sorted(rows, key=key) == sorted(expected, key=key), table


def test_the_default_organization_exists_with_revision_zero(migrated: None, settings: Settings) -> None:
    with connect(settings.database_url) as conn:
        org = default_org(conn)
        assert conn.execute("SELECT last FROM atlas.org_revision WHERE org_id = %s", (org,)).fetchone() == (0,)


# --------------------------------------------------------------------------- AC-CP3-02: the runtime role reads only


@pytest.mark.parametrize("table", CANONICAL)
def test_runtime_role_cannot_write_canonical_tables(migrated: None, settings: Settings, table: str) -> None:
    ident = sql.Identifier("atlas", table)
    first = sql.Identifier(metadata.tables[f"atlas.{table}"].columns[0].name)
    with connect(settings.database_url) as conn:
        conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident))  # reads are allowed
        for statement in (
            sql.SQL("INSERT INTO {} DEFAULT VALUES").format(ident),
            sql.SQL("UPDATE {} SET {} = {} WHERE false").format(ident, first, first),
            sql.SQL("DELETE FROM {} WHERE false").format(ident),
            sql.SQL("TRUNCATE {}").format(ident),
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


# --------------------------------------------------------------------------- AC-CP3-03: the ingestion role


def test_ingestion_role_inserts_and_closes_once(migrated: None, ingest_url: str) -> None:
    with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
        org = default_org(conn)
        insert_change_set(conn, org, 1)
        insert_change_set(
            conn,
            org,
            2,
            kind="steward_edit",
            channel="api",
            actor_kind="human",
            actor_namespace="oidc",
            actor_key="https://login.example.invalid/synthetic|steward-1",
        )
        eid = "0190f000-0000-7000-8000-000000000101"
        conn.execute("INSERT INTO atlas.entity (org_id, id, created_revision) VALUES (%s, %s, 1)", (org, eid))
        conn.execute(
            "INSERT INTO atlas.entity_version (org_id, entity_id, revision_from, type_key, category, record_state, "
            "display_name, lifecycle, attributes, trust_state, verification_state, taxonomy_version) VALUES "
            "(%s, %s, 1, 'core.team', 'core.team', 'present', 'Team', 'active', '{}', 'documented', 'unverified', "
            "'1.1.0')",
            (org, eid),
        )
        conn.execute("UPDATE atlas.entity_version SET revision_to = 2 WHERE entity_id = %s", (eid,))
        with pytest.raises(errors.IntegrityConstraintViolation, match="already set"), conn.transaction():
            conn.execute("UPDATE atlas.entity_version SET revision_to = 2 WHERE entity_id = %s", (eid,))
        with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE atlas.entity_version SET display_name = 'Renamed' WHERE entity_id = %s", (eid,))
        conn.execute("UPDATE atlas.org_revision SET last = 2 WHERE org_id = %s", (org,))


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM atlas.entity WHERE false",
        "DELETE FROM atlas.entity_version WHERE false",
        "TRUNCATE atlas.fact_assertion",
        "UPDATE atlas.change_set SET reason = reason WHERE false",
        "UPDATE atlas.org_revision SET org_id = org_id WHERE false",
        "INSERT INTO atlas.entity_type DEFAULT VALUES",
        "INSERT INTO atlas.organization DEFAULT VALUES",
        "UPDATE atlas.relationship_type SET status = status WHERE false",
        "CREATE TABLE atlas.ingest_ddl_attempt (id integer)",
        "CREATE TABLE public.ingest_ddl_attempt (id integer)",
        "CREATE TEMPORARY TABLE ingest_temp_attempt (id integer)",
        "ALTER TABLE atlas.entity ADD COLUMN x integer",
    ],
)
def test_ingestion_role_cannot_delete_truncate_rewrite_or_run_ddl(
    migrated: None, ingest_url: str, statement: str
) -> None:
    with (
        tx_connect(ingest_url) as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


def test_set_once_binds_the_owner_too(migrated: None, settings: Settings) -> None:
    with tx_connect(settings.migration_database_url) as conn, conn.transaction(force_rollback=True):
        org = default_org(conn)
        insert_change_set(conn, org, 1)
        conn.execute(
            "INSERT INTO atlas.entity (org_id, id, created_revision) VALUES (%s, %s, 1)",
            (org, "0190f000-0000-7000-8000-000000000102"),
        )
        conn.execute(
            "INSERT INTO atlas.entity_version (org_id, entity_id, revision_from, type_key, category, record_state, "
            "display_name, lifecycle, attributes, trust_state, verification_state, taxonomy_version) VALUES "
            "(%s, %s, 1, 'core.team', 'core.team', 'present', 'Team', 'active', '{}', 'documented', 'unverified', "
            "'1.1.0')",
            (org, "0190f000-0000-7000-8000-000000000102"),
        )
        with pytest.raises(errors.IntegrityConstraintViolation, match="only revision_to may change"):
            conn.execute("UPDATE atlas.entity_version SET display_name = 'Rewritten', revision_to = 1")


@pytest.mark.parametrize(("role", "database"), [("atlas_ingest", "atlas_test"), ("atlas_test_ingest", "atlas_db")])
def test_ingestion_roles_cannot_connect_to_the_other_environment(settings: Settings, role: str, database: str) -> None:
    with connect(settings.database_url) as conn:
        assert conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, database)).fetchone() == (False,)
        assert conn.execute("SELECT has_database_privilege(%s, %s, 'TEMP')", (role, "atlas_test")).fetchone() == (
            False,
        )


TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_ingestion_grants_follow_the_matrix(migrated: None, settings: Settings, ingest_url: str) -> None:
    role = settings.db_ingest_role
    held: set[tuple[str, str]] = set()
    updatable: set[tuple[str, str]] = set()
    with connect(settings.database_url) as conn:
        for table in CANONICAL:
            qualified = f"atlas.{table}"
            for privilege in TABLE_PRIVILEGES:
                (ok,) = conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, qualified, privilege)
                ).fetchone() or (False,)
                if ok:
                    held.add((table, privilege))
            for column in metadata.tables[qualified].columns:
                (ok,) = conn.execute(
                    "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')", (role, qualified, column.name)
                ).fetchone() or (False,)
                if ok:
                    updatable.add((table, column.name))
    assert held == {(t, "SELECT") for t in CANONICAL} | {
        (t, "INSERT") for t in schema_0002.INGEST_INSERT + schema_0002.INGEST_INSERT_AND_CLOSE
    }
    assert updatable == {(t, "revision_to") for t in schema_0002.INGEST_INSERT_AND_CLOSE} | {("org_revision", "last")}


# --------------------------------------------------------------------------- AC-CP3-04: server-set attribution


def test_change_set_time_and_role_are_set_by_the_server(migrated: None, ingest_url: str, settings: Settings) -> None:
    with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
        org = default_org(conn)
        insert_change_set(conn, org, 1, created_at="2001-01-01T00:00:00Z", db_role="atlas_test_app")
        created, role, now = conn.execute(
            "SELECT created_at, db_role, now() FROM atlas.change_set WHERE org_id = %s AND revision = 1", (org,)
        ).fetchone() or (None, None, None)
    assert created == now
    assert role == settings.db_ingest_role == make_url(ingest_url).username


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_kind": "human"},  # a service namespace recorded as a human
        {"actor_namespace": "oidc", "actor_key": "not-an-issuer-subject"},
        {"kind": "revert"},  # a revert names the revision it reverts
        {"kind": "nonsense"},
        {"input_hash": "md5:abc"},
    ],
)
def test_change_set_structure_is_enforced(migrated: None, ingest_url: str, overrides: dict[str, Any]) -> None:
    with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True), pytest.raises(errors.IntegrityError):
        insert_change_set(conn, default_org(conn), 1, **overrides)


def test_the_api_never_uses_the_ingestion_role(migrated: None, settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"diagnostics_enabled": True}))
    assert make_url(str(app.state.database.engine.url)).username == make_url(settings.database_url).username
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
