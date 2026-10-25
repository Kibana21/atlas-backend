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


def test_ingest_role_is_required_for_canonical_grants() -> None:
    context = SimpleNamespace(
        config=SimpleNamespace(attributes={migrations.INGEST_ROLE_ATTRIBUTE: "atlas_test_ingest"})
    )
    assert migrations.ingest_role(context) == "atlas_test_ingest"
    for attributes in ({}, {migrations.INGEST_ROLE_ATTRIBUTE: None}, {migrations.INGEST_ROLE_ATTRIBUTE: ""}):
        with pytest.raises(migrations.IngestRoleMissingError, match="G3-R"):
            migrations.ingest_role(SimpleNamespace(config=SimpleNamespace(attributes=attributes)))


def test_metadata_holds_the_canonical_tables() -> None:
    names = {t.name for t in migrations.metadata.sorted_tables}
    assert {
        "organization",
        "change_set",
        "entity",
        "entity_version",
        "relationship",
        "relationship_version",
        "external_identity",
        "entity_alias",
        "evidence",
        "fact_assertion",
        "graph_snapshot",
        "match_candidate",
        "reconciliation_case",
        "taxonomy_release",
        "relationship_evidence_kind",
    } <= names
