"""B-12: least privilege and isolation of the Atlas roles on real PostgreSQL (FR-30, FR-39, AC-07, AC-09)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors, sql
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.main import create_app
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.security]
ATLAS_ROLES = ("atlas_migrator", "atlas_app", "atlas_test_migrator", "atlas_test_app")


@pytest.fixture
def probe_table(migrated: None, settings: Settings) -> Iterator[sql.Identifier]:
    """A scratch table created by the migration role (as a migration would), dropped afterwards."""
    table = sql.Identifier("atlas", f"privilege_probe_{uuid.uuid4().hex[:12]}")
    with connect(settings.migration_database_url) as conn:
        conn.execute(sql.SQL("CREATE TABLE {} (id integer PRIMARY KEY, note text)").format(table))
    try:
        yield table
    finally:
        with connect(settings.migration_database_url) as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))


def test_runtime_role_cannot_run_ddl(migrated: None, settings: Settings) -> None:
    with connect(settings.database_url) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE atlas.runtime_ddl_attempt (id integer)")


def test_runtime_role_cannot_create_in_public(migrated: None, settings: Settings) -> None:
    with connect(settings.database_url) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE public.runtime_ddl_attempt (id integer)")


def test_runtime_role_cannot_create_temporary_tables(settings: Settings) -> None:
    with connect(settings.database_url) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("CREATE TEMPORARY TABLE runtime_temp_attempt (id integer)")


def test_runtime_role_reads_by_default_and_writes_only_with_an_explicit_grant(
    probe_table: sql.Identifier, settings: Settings
) -> None:
    with connect(settings.database_url) as app:
        assert app.execute(sql.SQL("SELECT count(*) FROM {}").format(probe_table)).fetchone() == (0,)
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute(sql.SQL("INSERT INTO {} VALUES (1, 'denied')").format(probe_table))
    with connect(settings.migration_database_url) as migrator:  # what a reviewed migration would do
        migrator.execute(sql.SQL("GRANT INSERT ON {} TO {}").format(probe_table, sql.Identifier(settings.db_app_role)))
    with connect(settings.database_url) as app:
        app.execute(sql.SQL("INSERT INTO {} VALUES (1, 'granted')").format(probe_table))
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute(sql.SQL("UPDATE {} SET note = 'denied'").format(probe_table))
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute(sql.SQL("DELETE FROM {}").format(probe_table))


@pytest.mark.parametrize("which", ["database_url", "migration_database_url"])
def test_test_roles_cannot_connect_to_the_local_database(settings: Settings, which: str) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        connect(getattr(settings, which), dbname="atlas_db").close()


@pytest.mark.parametrize(
    ("role", "database"),
    [
        ("atlas_app", "atlas_test"),
        ("atlas_migrator", "atlas_test"),
        ("atlas_test_app", "atlas_db"),
        ("atlas_test_migrator", "atlas_db"),
    ],
)
def test_no_cross_environment_connect_privilege(settings: Settings, role: str, database: str) -> None:
    with connect(settings.database_url) as conn:
        assert conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, database)).fetchone() == (False,)


def test_no_atlas_role_has_elevated_flags(settings: Settings) -> None:
    with connect(settings.database_url) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY 1",
            (list(ATLAS_ROLES),),
        ).fetchall()
    assert [r[0] for r in rows] == sorted(ATLAS_ROLES)
    assert all(tuple(row[1:]) == (False,) * 5 for row in rows)


def test_public_has_no_connect_or_temp_on_atlas_databases(settings: Settings) -> None:
    with connect(settings.database_url) as conn:
        acls = conn.execute(
            "SELECT datname, datacl::text FROM pg_database WHERE datname IN ('atlas_db', 'atlas_test') ORDER BY 1"
        ).fetchall()
    assert [name for name, _ in acls] == ["atlas_db", "atlas_test"]
    for _, acl in acls:
        assert not any(entry.startswith("=") for entry in acl.strip("{}").split(","))


def test_build_info_reports_the_revision_read_by_the_runtime_role(migrated: None, settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"diagnostics_enabled": True}))) as client:
        body = client.get("/api/v1/diagnostics/build").json()
    assert body["db_migration_revision"] == "0001_baseline"


@pytest.mark.ci_auth
@pytest.mark.skipif(
    os.environ.get("ATLAS_EXPECT_PASSWORD_AUTH") != "true",
    reason="CI only (FR-39): local loopback uses trust authentication (DEC-084); no wrong-password probes locally",
)
@pytest.mark.parametrize("role", ATLAS_ROLES)
@pytest.mark.parametrize("password", ["wrong-password", ""])
def test_ci_rejects_wrong_or_missing_passwords(settings: Settings, role: str, password: str) -> None:
    url = make_url(settings.database_url)
    database = "atlas_test" if role.startswith("atlas_test_") else "atlas_db"
    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(
            host=url.host, port=url.port or 5432, user=role, password=password, dbname=database, connect_timeout=5
        ).close()
