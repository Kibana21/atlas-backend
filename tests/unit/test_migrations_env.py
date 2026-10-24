"""atlas.persistence.migrations: pure parts of the Alembic environment (B-10). DB runs are integration tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from atlas.persistence import migrations

pytestmark = pytest.mark.unit


def test_only_schema_atlas_is_compared() -> None:
    assert migrations.include_name("atlas", "schema", {}) is True
    assert migrations.include_name("public", "schema", {}) is False
    assert migrations.include_name(None, "schema", {}) is False  # the default schema is not reflected twice
    assert migrations.include_name("anything", "table", {}) is True


def test_metadata_uses_schema_atlas_and_the_naming_convention() -> None:
    assert migrations.metadata.schema == "atlas"
    assert migrations.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert set(migrations.NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


def test_offline_mode_is_refused() -> None:
    context = SimpleNamespace(is_offline_mode=lambda: True)
    with pytest.raises(RuntimeError, match="offline"):
        migrations.run(context)


def test_app_role_comes_from_settings() -> None:
    context = SimpleNamespace(config=SimpleNamespace(attributes={migrations.APP_ROLE_ATTRIBUTE: "atlas_test_app"}))
    assert migrations.app_role(context) == "atlas_test_app"
