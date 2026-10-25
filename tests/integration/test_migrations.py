"""B-11 / AC-CP3-01: migration round trip on atlas_test as atlas_test_migrator (FR-28, FR-29, AC-06)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.persistence.canonical import schema_0002
from atlas.persistence.migrations import SETTINGS_ATTRIBUTE, IngestRoleMissingError, metadata
from tests.integration.conftest import connect

pytestmark = pytest.mark.integration


def _atlas_tables(settings: Settings) -> list[tuple[str, str]]:
    with connect(settings.migration_database_url) as conn:
        return conn.execute(
            "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'atlas' ORDER BY 1"
        ).fetchall()


def _revision(settings: Settings) -> list[tuple[str]]:
    with connect(settings.migration_database_url) as conn:
        return conn.execute("SELECT version_num FROM atlas.alembic_version").fetchall()


def test_single_head(alembic_config: Config) -> None:
    assert ScriptDirectory.from_config(alembic_config).get_heads() == ["0002_canonical_graph"]


def test_upgrade_downgrade_upgrade_round_trip(alembic_config: Config, settings: Settings) -> None:
    command.upgrade(alembic_config, "head")
    assert _revision(settings) == [("0002_canonical_graph",)]
    command.downgrade(alembic_config, "0001_baseline")
    assert _revision(settings) == [("0001_baseline",)]
    assert [t for t, _ in _atlas_tables(settings)] == ["alembic_version"]  # 0002 downgrade leaves nothing behind
    command.downgrade(alembic_config, "base")
    assert _revision(settings) == []
    command.upgrade(alembic_config, "head")
    assert _revision(settings) == [("0002_canonical_graph",)]
    command.check(alembic_config)  # no drift between metadata and schema `atlas`


def test_every_table_is_owned_by_the_migrator_and_no_extension_is_created(migrated: None, settings: Settings) -> None:
    owner = make_url(settings.migration_database_url).username
    tables = _atlas_tables(settings)
    assert {t for t, _ in tables} == {"alembic_version", *(t.name for t in metadata.sorted_tables)}
    assert {o for t, o in tables if t != "alembic_version"} == {owner}
    # alembic_version keeps the owner that created it: the ATLAS-001 migrator, or the single-user account
    assert dict(tables)["alembic_version"] in {owner, "atlas_test_migrator"}
    with connect(settings.migration_database_url) as conn:
        assert conn.execute("SELECT extname FROM pg_extension ORDER BY 1").fetchall() == [("plpgsql",)]
        triggers = conn.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'atlas' AND NOT t.tgisinternal ORDER BY 1"
        ).fetchall()
    assert [t for (t,) in triggers] == sorted(
        ["change_set_server_fields", *(f"{t}_set_once" for t in schema_0002.VERSIONED_TABLES)]
    )


def test_migration_fails_without_the_ingestion_role(alembic_config: Config, settings: Settings) -> None:
    command.downgrade(alembic_config, "0001_baseline")
    try:
        broken = Config(alembic_config.config_file_name)
        broken.attributes[SETTINGS_ATTRIBUTE] = settings.model_copy(update={"db_ingest_role": "atlas_no_such_role"})
        with pytest.raises(IngestRoleMissingError, match="does not exist"):
            command.upgrade(broken, "head")
        assert _revision(settings) == [("0001_baseline",)]  # nothing applied
    finally:
        command.upgrade(alembic_config, "head")
