"""B-11: migration round trip on atlas_test as atlas_test_migrator (FR-28, FR-29, AC-06)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
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
    assert ScriptDirectory.from_config(alembic_config).get_heads() == ["0001_baseline"]


def test_upgrade_downgrade_upgrade_round_trip(alembic_config: Config, settings: Settings) -> None:
    command.upgrade(alembic_config, "head")
    assert _revision(settings) == [("0001_baseline",)]
    command.downgrade(alembic_config, "base")
    assert _revision(settings) == []
    command.upgrade(alembic_config, "head")
    assert _revision(settings) == [("0001_baseline",)]
    command.check(alembic_config)  # no drift between metadata and schema `atlas`


def test_schema_holds_only_the_version_table_owned_by_the_migrator(migrated: None, settings: Settings) -> None:
    assert _atlas_tables(settings) == [("alembic_version", make_url(settings.migration_database_url).username)]
