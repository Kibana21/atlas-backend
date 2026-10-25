"""Canonical store tables as created by migration ``0002_canonical_graph`` (ADR-0001, frozen).

Never edit these definitions: migration 0002 builds its tables from them, and the Alembic metadata uses them so
``alembic check`` proves the database has no drift. A later schema change is a new migration and a new module.

Database constraints cover structure and security only (keys, organization-scoped composite FKs, enumerations,
set-once versions, column privileges). Architecture rules stay in the domain validator (atlas.domain.canonical.rules).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

SCHEMA = "atlas"

LIFECYCLES = ("active", "deprecated", "retired")
TRUST_STATES = ("discovered", "documented", "curated", "inferred", "proposed", "stale", "conflicted")
EVIDENCE_KINDS = ("provider_configuration", "contract", "documentation", "human_assertion", "inferred")
CHANGE_SET_KINDS = ("fixture_load", "import", "steward_edit", "merge", "split", "discovery_sync", "migration", "revert")
CHANNELS = ("fixture_cli", "import", "api", "connector", "migration", "system_job")
SHA256 = r"^sha256:[0-9a-f]{64}$"
TAXONOMY_VERSION = r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$"
VERSIONED_TABLES = ("entity_version", "relationship_version", "external_identity", "entity_alias", "fact_assertion")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _uuid(name: str, *, nullable: bool = False) -> Column[str]:
    return Column(name, UUID(as_uuid=False), nullable=nullable)


def _revision(name: str, *, nullable: bool = False) -> Column[int]:
    return Column(name, BigInteger, nullable=nullable)


def _taxonomy_version() -> Column[str]:
    return Column("taxonomy_version", Text, ForeignKey(f"{SCHEMA}.taxonomy_release.version"), nullable=False)


def _verification() -> list[Any]:
    return [
        Column("verification_state", Text, nullable=False),
        Column("verified_by_namespace", Text, ForeignKey(f"{SCHEMA}.actor_namespace.namespace"), nullable=True),
        Column("verified_by_key", Text, nullable=True),
        Column("verified_at", TIMESTAMP(timezone=True), nullable=True),
        Column("expires_at", TIMESTAMP(timezone=True), nullable=True),
        CheckConstraint(_in("verification_state", ("unverified", "verified")), name="verification_state"),
        CheckConstraint(
            "(verification_state = 'verified' AND verified_by_namespace IS NOT NULL AND verified_by_key IS NOT NULL "
            "AND verified_at IS NOT NULL) OR (verification_state = 'unverified' AND verified_by_namespace IS NULL "
            "AND verified_by_key IS NULL AND verified_at IS NULL AND expires_at IS NULL)",
            name="verification_record",
        ),
    ]


def _versioned(table: str) -> list[Any]:
    """``[revision_from, revision_to)``; both revisions are change sets of the same organization."""
    return [
        _revision("revision_from"),
        _revision("revision_to", nullable=True),
        CheckConstraint("revision_to IS NULL OR revision_to > revision_from", name="revision_interval"),
        ForeignKeyConstraint(
            ["org_id", "revision_from"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name=f"fk_{table}_revision_from_change_set",
        ),
        ForeignKeyConstraint(
            ["org_id", "revision_to"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name=f"fk_{table}_revision_to_change_set",
        ),
    ]


def _entity_fk(table: str, column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", column], [f"{SCHEMA}.entity.org_id", f"{SCHEMA}.entity.id"], name=f"fk_{table}_{column}_entity"
    )


def define(metadata: MetaData) -> None:
    """Register every canonical table on ``metadata`` (schema ``atlas``)."""
    _reference_tables(metadata)
    _change_tables(metadata)
    _fact_tables(metadata)
    _provenance_tables(metadata)


def _reference_tables(md: MetaData) -> None:
    """Seeded from the pinned taxonomy by migrations only (ADR-0017 §1, ADR-0019)."""
    Table(
        "taxonomy_release",
        md,
        Column("version", Text, primary_key=True),
        Column("status", Text, nullable=False),
        CheckConstraint(f"version ~ '{TAXONOMY_VERSION}'", name="version_format"),
        CheckConstraint(_in("status", ("accepted", "synthetic_test_only")), name="status"),
    )
    Table(
        "provider",
        md,
        Column("key", Text, primary_key=True),
        Column("display_name", Text, nullable=False),
        Column("provider_kind", Text, nullable=False),
        Column("requires_platform", Boolean, nullable=False),
        _taxonomy_version(),
    )
    Table(
        "category",
        md,
        Column("key", Text, primary_key=True),
        Column("description", Text, nullable=False),
        _taxonomy_version(),
    )
    Table(
        "identity_namespace",
        md,
        Column("namespace", Text, primary_key=True),
        Column("provider", Text, nullable=True),
        Column("status", Text, nullable=False),
        Column("normalization_rule", Text, nullable=True),
        Column("case_rule", Text, nullable=True),
        _taxonomy_version(),
        CheckConstraint(_in("status", ("implemented", "validation_only", "planned", "reserved")), name="status"),
        CheckConstraint(
            "(status IN ('implemented', 'validation_only')) = (normalization_rule IS NOT NULL)", name="rule"
        ),
    )
    Table(
        "native_type_mapping",
        md,
        Column("taxonomy_version", Text, ForeignKey(f"{SCHEMA}.taxonomy_release.version"), nullable=False),
        Column("provider", Text, ForeignKey(f"{SCHEMA}.provider.key"), nullable=False),
        Column("native_type", Text, nullable=False),
        Column("category", Text, ForeignKey(f"{SCHEMA}.category.key"), nullable=False),
        PrimaryKeyConstraint("taxonomy_version", "provider", "native_type"),
    )
    Table(
        "actor_namespace",
        md,
        Column("namespace", Text, primary_key=True),
        Column("actor_kind", Text, nullable=False),
        Column("key_pattern", Text, nullable=False),
        _taxonomy_version(),
        CheckConstraint(_in("actor_kind", ("human", "service", "system")), name="actor_kind"),
        UniqueConstraint("namespace", "actor_kind"),
    )
    Table(
        "entity_type",
        md,
        Column("type_key", Text, primary_key=True),
        Column("kind", Text, nullable=False),
        Column("category", Text, nullable=False),
        Column("provider", Text, nullable=True),
        Column("environment_scope", Text, nullable=False),
        Column("native_type", Text, nullable=True),
        _taxonomy_version(),
        CheckConstraint(_in("kind", ("logical", "physical")), name="kind"),
        CheckConstraint(_in("environment_scope", ("none", "optional", "required")), name="environment_scope"),
    )
    Table(
        "relationship_type",
        md,
        Column("rel_type", Text, primary_key=True),
        Column("status", Text, nullable=False),
        Column("cardinality", Text, nullable=False),
        Column("discriminator", Text, nullable=True),
        Column("cross_environment_allowed", Boolean, nullable=True),
        Column("logical_only", Boolean, nullable=False),
        _taxonomy_version(),
        CheckConstraint(_in("status", ("active", "reserved")), name="status"),
        CheckConstraint(_in("cardinality", ("one_parent", "many_to_many")), name="cardinality"),
    )
    Table(
        "relationship_evidence_kind",
        md,
        Column("rel_type", Text, ForeignKey(f"{SCHEMA}.relationship_type.rel_type"), nullable=False),
        Column("evidence_kind", Text, nullable=False),
        _taxonomy_version(),
        PrimaryKeyConstraint("rel_type", "evidence_kind"),
        CheckConstraint(_in("evidence_kind", EVIDENCE_KINDS), name="evidence_kind"),
    )
    Table(
        "relationship_rule",
        md,
        Column("rel_type", Text, ForeignKey(f"{SCHEMA}.relationship_type.rel_type"), nullable=False),
        Column("from_type", Text, ForeignKey(f"{SCHEMA}.entity_type.type_key"), nullable=False),
        Column("to_type", Text, ForeignKey(f"{SCHEMA}.entity_type.type_key"), nullable=False),
        _taxonomy_version(),
        PrimaryKeyConstraint("rel_type", "from_type", "to_type"),
    )
    Table(
        "attribute_rule",
        md,
        Column("owner_kind", Text, nullable=False),
        Column("owner_key", Text, nullable=False),
        Column("attribute", Text, nullable=False),
        Column("required", Boolean, nullable=False),
        Column("allowed_values", JSONB, nullable=True),
        _taxonomy_version(),
        PrimaryKeyConstraint("owner_kind", "owner_key", "attribute"),
        CheckConstraint(_in("owner_kind", ("entity_type", "relationship_type")), name="owner_kind"),
    )


def _change_tables(md: MetaData) -> None:
    """Organizations, the revision counter and change sets (ADR-0016, ADR-0008 §3, §10)."""
    Table(
        "organization",
        md,
        _uuid("id"),
        Column("key", Text, nullable=False),
        Column("display_name", Text, nullable=False),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("key"),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 200", name="display_name"),
        CheckConstraint("key ~ '^[a-z0-9][a-z0-9:_-]*$'", name="key_format"),
    )
    Table(
        "org_revision",
        md,
        Column("org_id", UUID(as_uuid=False), ForeignKey(f"{SCHEMA}.organization.id"), primary_key=True),
        Column("last", BigInteger, nullable=False, server_default=text("0")),
        CheckConstraint("last >= 0", name="last"),
    )
    Table(
        "change_set",
        md,
        Column("org_id", UUID(as_uuid=False), ForeignKey(f"{SCHEMA}.organization.id"), nullable=False),
        _revision("revision"),
        Column("kind", Text, nullable=False),
        Column("channel", Text, nullable=False),
        Column("actor_kind", Text, nullable=False),
        Column("actor_namespace", Text, nullable=False),
        Column("actor_key", Text, nullable=False),
        Column("actor_label", Text, nullable=True),
        Column("source_ref", Text, nullable=False),
        Column("input_hash", Text, nullable=True),
        _revision("reverts_revision", nullable=True),
        Column("reason", Text, nullable=False),
        Column("change_reference", Text, nullable=True),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False),  # set by the server (trigger)
        Column("db_role", Text, nullable=False),  # set by the server (trigger): session_user
        PrimaryKeyConstraint("org_id", "revision"),
        ForeignKeyConstraint(
            ["actor_namespace", "actor_kind"],
            [f"{SCHEMA}.actor_namespace.namespace", f"{SCHEMA}.actor_namespace.actor_kind"],
            name="fk_change_set_actor_namespace_kind",
        ),
        ForeignKeyConstraint(
            ["org_id", "reverts_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_change_set_reverts_revision_change_set",
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint(_in("kind", CHANGE_SET_KINDS), name="kind"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint("(kind = 'revert') = (reverts_revision IS NOT NULL)", name="revert"),
        CheckConstraint("reverts_revision IS NULL OR reverts_revision < revision", name="reverts_earlier"),
        CheckConstraint(
            r"(actor_namespace <> 'oidc' OR actor_key ~ '^https://\S+\|\S+$') AND "
            r"(actor_namespace NOT IN ('atlas.service', 'atlas.system') "
            r"OR actor_key ~ '^[a-z][a-z0-9-]*(\.[a-z0-9-]+)*$')",
            name="actor_key_format",
        ),
        CheckConstraint("char_length(actor_key) <= 512 AND char_length(source_ref) BETWEEN 1 AND 300", name="lengths"),
        CheckConstraint("char_length(reason) <= 2000", name="reason_length"),
        CheckConstraint(f"input_hash IS NULL OR input_hash ~ '{SHA256}'", name="input_hash"),
        Index("ix_change_set_org_id_input_hash", "org_id", "input_hash"),
        Index("ix_change_set_org_id_actor", "org_id", "actor_namespace", "actor_key"),
    )
    Table(
        "graph_snapshot",
        md,
        _uuid("id"),
        _uuid("org_id"),
        _revision("revision"),
        Column("label", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("graph_hash", Text, nullable=False),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "label"),
        ForeignKeyConstraint(
            ["org_id", "revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_graph_snapshot_revision_change_set",
        ),
        CheckConstraint("status = 'published'", name="status"),
        CheckConstraint(f"graph_hash ~ '{SHA256}'", name="graph_hash"),
    )


def _fact_tables(md: MetaData) -> None:
    """Identity rows (insert-only) and their versions (ADR-0001, ADR-0008 §2)."""
    Table(
        "entity",
        md,
        _uuid("id"),
        _uuid("org_id"),
        _revision("created_revision"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "created_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_entity_created_revision_change_set",
        ),
    )
    Table(
        "entity_version",
        md,
        _uuid("org_id"),
        _uuid("entity_id"),
        *_versioned("entity_version"),
        Column("type_key", Text, ForeignKey(f"{SCHEMA}.entity_type.type_key"), nullable=False),
        Column("category", Text, nullable=False),
        Column("provider", Text, ForeignKey(f"{SCHEMA}.provider.key"), nullable=True),
        Column("native_type", Text, nullable=True),
        Column("record_state", Text, nullable=False),
        _uuid("merged_into", nullable=True),
        Column("display_name", Text, nullable=False),
        Column("lifecycle", Text, nullable=False),
        Column("environment_scope", Text, nullable=True),
        _uuid("environment_id", nullable=True),
        _uuid("container_id", nullable=True),
        Column("attributes", JSONB, nullable=False),
        Column("trust_state", Text, nullable=False),
        *_verification(),
        _taxonomy_version(),
        PrimaryKeyConstraint("org_id", "entity_id", "revision_from"),
        _entity_fk("entity_version", "entity_id"),
        _entity_fk("entity_version", "merged_into"),
        _entity_fk("entity_version", "environment_id"),
        _entity_fk("entity_version", "container_id"),
        CheckConstraint(_in("record_state", ("present", "removed", "merged")), name="record_state"),
        CheckConstraint("(record_state = 'merged') = (merged_into IS NOT NULL)", name="merged"),
        CheckConstraint(_in("lifecycle", LIFECYCLES), name="lifecycle"),
        CheckConstraint(_in("trust_state", TRUST_STATES), name="trust_state"),
        CheckConstraint(
            "environment_scope IS NULL OR environment_scope IN ('single', 'shared', 'unknown')",
            name="environment_scope",
        ),
        CheckConstraint("(environment_scope = 'single') = (environment_id IS NOT NULL)", name="environment_id"),
        CheckConstraint("native_type IS NULL OR provider IS NOT NULL", name="native_type_provider"),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 200", name="display_name"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes"),
        CheckConstraint("entity_id <> container_id AND entity_id <> environment_id", name="not_self"),
        Index(
            "uq_entity_version_current",
            "org_id",
            "entity_id",
            unique=True,
            postgresql_where=text("revision_to IS NULL"),
        ),
        Index("ix_entity_version_current_type", "org_id", "type_key", postgresql_where=text("revision_to IS NULL")),
        Index(
            "ix_entity_version_current_provider",
            "org_id",
            "provider",
            "category",
            postgresql_where=text("revision_to IS NULL"),
        ),
        Index(
            "ix_entity_version_current_container",
            "org_id",
            "container_id",
            postgresql_where=text("revision_to IS NULL"),
        ),
        Index(
            "ix_entity_version_current_environment",
            "org_id",
            "environment_id",
            postgresql_where=text("revision_to IS NULL"),
        ),
    )
    Table(
        "relationship",
        md,
        _uuid("id"),
        _uuid("org_id"),
        Column("rel_type", Text, ForeignKey(f"{SCHEMA}.relationship_type.rel_type"), nullable=False),
        _uuid("from_id"),
        _uuid("to_id"),
        Column("discriminator", Text, nullable=False, server_default=text("''")),
        _revision("created_revision"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "id", "rel_type"),
        UniqueConstraint("org_id", "rel_type", "from_id", "to_id", "discriminator"),
        _entity_fk("relationship", "from_id"),
        _entity_fk("relationship", "to_id"),
        ForeignKeyConstraint(
            ["org_id", "created_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_relationship_created_revision_change_set",
        ),
        CheckConstraint("from_id <> to_id", name="not_self"),
        CheckConstraint("char_length(discriminator) <= 100", name="discriminator"),
        Index("ix_relationship_org_id_from_id_rel_type", "org_id", "from_id", "rel_type"),
        Index("ix_relationship_org_id_to_id_rel_type", "org_id", "to_id", "rel_type"),
    )
    Table(
        "relationship_version",
        md,
        _uuid("org_id"),
        _uuid("relationship_id"),
        *_versioned("relationship_version"),
        Column("record_state", Text, nullable=False),
        Column("lifecycle", Text, nullable=False),
        _uuid("environment_id", nullable=True),
        Column("cross_environment", Boolean, nullable=False),
        Column("shared_endpoint", Boolean, nullable=False),
        Column("cross_provider", Boolean, nullable=False),
        Column("attributes", JSONB, nullable=False),
        Column("trust_state", Text, nullable=False),
        *_verification(),
        _taxonomy_version(),
        PrimaryKeyConstraint("org_id", "relationship_id", "revision_from"),
        ForeignKeyConstraint(
            ["org_id", "relationship_id"],
            [f"{SCHEMA}.relationship.org_id", f"{SCHEMA}.relationship.id"],
            name="fk_relationship_version_relationship_id_relationship",
        ),
        _entity_fk("relationship_version", "environment_id"),
        CheckConstraint(_in("record_state", ("present", "removed")), name="record_state"),
        CheckConstraint(_in("lifecycle", LIFECYCLES), name="lifecycle"),
        CheckConstraint(_in("trust_state", TRUST_STATES), name="trust_state"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes"),
        CheckConstraint("NOT (cross_environment AND environment_id IS NOT NULL)", name="cross_environment"),
        Index(
            "uq_relationship_version_current",
            "org_id",
            "relationship_id",
            unique=True,
            postgresql_where=text("revision_to IS NULL"),
        ),
    )


def _provenance_tables(md: MetaData) -> None:
    """Identities, aliases, evidence, assertions and the reconciliation shells (ADR-0003, ADR-0018)."""
    Table(
        "external_identity",
        md,
        _uuid("id"),
        _uuid("org_id"),
        _uuid("entity_id"),
        Column("namespace", Text, nullable=False),
        Column("original_key", Text, nullable=False),
        Column("normalized_key", Text, nullable=False),
        Column("normalization_version", Text, nullable=False),
        Column("key_parts", JSONB, nullable=False),
        Column("match_method", Text, nullable=False),
        *_versioned("external_identity"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id"),
        _entity_fk("external_identity", "entity_id"),
        CheckConstraint(r"namespace ~ '^[a-z][a-z0-9]*(\.[a-z0-9_*-]+)*$'", name="namespace"),
        CheckConstraint(
            _in("match_method", ("provider_id", "approved_alias", "org_key", "steward")), name="match_method"
        ),
        CheckConstraint("char_length(original_key) <= 2048 AND char_length(normalized_key) <= 2048", name="key_length"),
        CheckConstraint("jsonb_typeof(key_parts) = 'object'", name="key_parts"),
        Index(
            "uq_external_identity_current",
            "org_id",
            "namespace",
            "normalized_key",
            unique=True,
            postgresql_where=text("revision_to IS NULL"),
        ),
        Index("ix_external_identity_org_id_entity_id", "org_id", "entity_id"),
    )
    Table(
        "entity_alias",
        md,
        _uuid("id"),
        _uuid("org_id"),
        _uuid("entity_id"),
        Column("alias_original", Text, nullable=False),
        Column("alias_normalized", Text, nullable=False),
        Column("scope_type", Text, ForeignKey(f"{SCHEMA}.entity_type.type_key"), nullable=False),
        _uuid("scope_environment_id", nullable=True),
        Column("status", Text, nullable=False),
        Column("approved_by_namespace", Text, ForeignKey(f"{SCHEMA}.actor_namespace.namespace"), nullable=True),
        Column("approved_by_key", Text, nullable=True),
        *_versioned("entity_alias"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id"),
        _entity_fk("entity_alias", "entity_id"),
        _entity_fk("entity_alias", "scope_environment_id"),
        CheckConstraint(_in("status", ("proposed", "approved", "retired")), name="status"),
        CheckConstraint(
            "status <> 'approved' OR (approved_by_namespace IS NOT NULL AND approved_by_key IS NOT NULL)",
            name="approved_by",
        ),
        Index(
            "uq_entity_alias_approved",
            "org_id",
            "scope_type",
            "scope_environment_id",
            "alias_normalized",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("status = 'approved' AND revision_to IS NULL"),
        ),
    )
    Table(
        "evidence",
        md,
        _uuid("id"),
        _uuid("org_id"),
        Column("kind", Text, nullable=False),
        Column("source_ref", Text, nullable=False),
        Column("artifact_ref", Text, nullable=True),
        Column("artifact_hash", Text, nullable=True),
        Column("method", Text, nullable=False),
        Column("confidence", Double, nullable=False),
        Column("observed_at", TIMESTAMP(timezone=True), nullable=False),
        Column("valid_from", TIMESTAMP(timezone=True), nullable=True),
        Column("valid_to", TIMESTAMP(timezone=True), nullable=True),
        _revision("change_revision"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id", "kind"),
        ForeignKeyConstraint(
            ["org_id", "change_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_evidence_change_revision_change_set",
        ),
        CheckConstraint(_in("kind", EVIDENCE_KINDS), name="kind"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint(f"artifact_hash IS NULL OR artifact_hash ~ '{SHA256}'", name="artifact_hash"),
        CheckConstraint(
            "char_length(source_ref) BETWEEN 1 AND 300 AND char_length(method) BETWEEN 1 AND 200", name="lengths"
        ),
        Index("ix_evidence_org_id_change_revision", "org_id", "change_revision"),
    )
    Table(
        "fact_assertion",
        md,
        _uuid("id"),
        _uuid("org_id"),
        Column("fact_kind", Text, nullable=False),
        _uuid("fact_id"),
        _uuid("entity_fact_id", nullable=True),
        _uuid("relationship_fact_id", nullable=True),
        Column("rel_type", Text, nullable=True),
        Column("field_path", Text, nullable=True),
        Column("asserted_value", JSONB, nullable=True),
        _uuid("evidence_id"),
        Column("evidence_kind", Text, nullable=False),
        *_versioned("fact_assertion"),
        PrimaryKeyConstraint("id"),
        _entity_fk("fact_assertion", "entity_fact_id"),
        ForeignKeyConstraint(
            ["org_id", "relationship_fact_id", "rel_type"],
            [f"{SCHEMA}.relationship.org_id", f"{SCHEMA}.relationship.id", f"{SCHEMA}.relationship.rel_type"],
            name="fk_fact_assertion_relationship_fact_id_relationship",
        ),
        ForeignKeyConstraint(
            ["rel_type", "evidence_kind"],
            [f"{SCHEMA}.relationship_evidence_kind.rel_type", f"{SCHEMA}.relationship_evidence_kind.evidence_kind"],
            name="fk_fact_assertion_allowed_evidence_kind",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id", "evidence_kind"],
            [f"{SCHEMA}.evidence.org_id", f"{SCHEMA}.evidence.id", f"{SCHEMA}.evidence.kind"],
            name="fk_fact_assertion_evidence_id_evidence",
        ),
        CheckConstraint(
            "(fact_kind = 'entity' AND entity_fact_id = fact_id AND relationship_fact_id IS NULL AND rel_type IS NULL) "
            "OR (fact_kind = 'relationship' AND relationship_fact_id = fact_id AND entity_fact_id IS NULL "
            "AND rel_type IS NOT NULL)",
            name="fact_reference",
        ),
        CheckConstraint("field_path IS NOT NULL OR asserted_value IS NULL", name="existence_has_no_value"),
        Index(
            "ix_fact_assertion_current", "org_id", "fact_id", "field_path", postgresql_where=text("revision_to IS NULL")
        ),
    )
    Table(
        "reconciliation_case",
        md,
        _uuid("id"),
        _uuid("org_id"),
        Column("kind", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("details", JSONB, nullable=False),
        _revision("created_revision"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("org_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "created_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_reconciliation_case_created_revision_change_set",
        ),
        CheckConstraint(_in("kind", ("identity_conflict", "value_conflict", "match_review")), name="kind"),
        CheckConstraint(_in("status", ("open", "resolved")), name="status"),
    )
    Table(
        "match_candidate",
        md,
        _uuid("id"),
        _uuid("org_id"),
        _uuid("case_id", nullable=True),
        _uuid("entity_id"),
        Column("score", Double, nullable=False),
        Column("status", Text, nullable=False),
        Column("details", JSONB, nullable=False),
        _revision("created_revision"),
        PrimaryKeyConstraint("id"),
        ForeignKeyConstraint(
            ["org_id", "case_id"],
            [f"{SCHEMA}.reconciliation_case.org_id", f"{SCHEMA}.reconciliation_case.id"],
            name="fk_match_candidate_case_id_reconciliation_case",
        ),
        _entity_fk("match_candidate", "entity_id"),
        ForeignKeyConstraint(
            ["org_id", "created_revision"],
            [f"{SCHEMA}.change_set.org_id", f"{SCHEMA}.change_set.revision"],
            name="fk_match_candidate_created_revision_change_set",
        ),
        CheckConstraint("score >= 0 AND score <= 1", name="score"),
        CheckConstraint(_in("status", ("open", "accepted", "rejected")), name="status"),
    )


# --------------------------------------------------------------------------- the two approved triggers (ADR-0008)

SET_ONCE_FUNCTION = f"""
CREATE FUNCTION {SCHEMA}.set_once_revision_to() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF OLD.revision_to IS NOT NULL THEN
    RAISE EXCEPTION 'canonical version rows are immutable: revision_to is already set'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.revision_to IS NULL THEN
    RAISE EXCEPTION 'canonical version rows are immutable: revision_to can only be set'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (to_jsonb(NEW) - 'revision_to') IS DISTINCT FROM (to_jsonb(OLD) - 'revision_to') THEN
    RAISE EXCEPTION 'canonical version rows are immutable: only revision_to may change'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$
"""

CHANGE_SET_FUNCTION = f"""
CREATE FUNCTION {SCHEMA}.change_set_server_fields() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  NEW.created_at := now();
  NEW.db_role := session_user;
  RETURN NEW;
END $$
"""


def trigger_statements() -> list[str]:
    statements = [SET_ONCE_FUNCTION, CHANGE_SET_FUNCTION]
    statements += [
        f"CREATE TRIGGER {table}_set_once BEFORE UPDATE ON {SCHEMA}.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_once_revision_to()"
        for table in VERSIONED_TABLES
    ]
    statements.append(
        f"CREATE TRIGGER change_set_server_fields BEFORE INSERT ON {SCHEMA}.change_set "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.change_set_server_fields()"
    )
    return statements


# --------------------------------------------------------------------------- ingestion grants (DEC-044 amendment §5)

INGEST_READ_ONLY = (
    "taxonomy_release",
    "organization",
    "provider",
    "category",
    "identity_namespace",
    "native_type_mapping",
    "actor_namespace",
    "entity_type",
    "relationship_type",
    "relationship_evidence_kind",
    "relationship_rule",
    "attribute_rule",
)
INGEST_INSERT = (
    "change_set",
    "graph_snapshot",
    "evidence",
    "entity",
    "relationship",
    "match_candidate",
    "reconciliation_case",
)
INGEST_INSERT_AND_CLOSE = VERSIONED_TABLES


def grant_statements(role: str) -> list[str]:
    """Per-table grants for the ingestion role. ``role`` is quoted as an identifier by the caller."""
    out = [f"GRANT SELECT ON {SCHEMA}.{t} TO {role}" for t in INGEST_READ_ONLY]
    out.append(f"GRANT SELECT, UPDATE (last) ON {SCHEMA}.org_revision TO {role}")
    out += [f"GRANT SELECT, INSERT ON {SCHEMA}.{t} TO {role}" for t in INGEST_INSERT]
    out += [f"GRANT SELECT, INSERT, UPDATE (revision_to) ON {SCHEMA}.{t} TO {role}" for t in INGEST_INSERT_AND_CLOSE]
    return out
