"""Canonical graph store (ATLAS-002 CP3; ADR-0001, ADR-0003, ADR-0008, ADR-0016, ADR-0017, ADR-0018, ADR-0019).

Revision ID: 0002_canonical_graph
Revises: 0001_baseline
Create Date: 2026-10-25

Creates the canonical tables of ``atlas.persistence.canonical.schema_0002`` (keys, organization-scoped composite
FKs, partial unique indexes, ``CHECK`` constraints; no extension), the two approved triggers (set-once
``revision_to``; server-set ``created_at``/``db_role`` on change sets), the per-table grants of the DEC-044
amendment to the ingestion role, the taxonomy reference rows from the packaged artifact, and the default
organization (ADR-0016). The runtime role keeps only ``SELECT`` (schema default privileges, ATLAS-001).

It **fails** when the ingestion role is not configured or does not exist; it never skips the grants. The
downgrade drops everything it created and is meant for empty databases only (ADR-0008 §7).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from atlas.core.ids import uuid7
from atlas.domain.canonical import taxonomy
from atlas.persistence.canonical import schema_0002
from atlas.persistence.migrations import NAMING_CONVENTION, IngestRoleMissingError, ingest_role

revision: str = "0002_canonical_graph"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_ORDER = (
    "taxonomy_release",
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
TAXONOMY_VERSION = "1.1.0"  # frozen: a later taxonomy version is seeded by a later migration
DEFAULT_ORGANIZATION = {"key": "default", "display_name": "Default organization"}


def _tables() -> sa.MetaData:
    metadata = sa.MetaData(schema=schema_0002.SCHEMA, naming_convention=NAMING_CONVENTION)
    schema_0002.define(metadata)
    return metadata


def upgrade() -> None:
    bind = op.get_bind()
    role = ingest_role(context)
    if bind.execute(sa.text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar_one() != 1:
        raise IngestRoleMissingError(f"ingestion role {role!r} does not exist: the server owner creates it (gate G3-R)")
    metadata = _tables()
    metadata.create_all(bind)
    for statement in schema_0002.trigger_statements():
        op.execute(statement)
    quoted = bind.dialect.identifier_preparer.quote(role)
    for statement in schema_0002.grant_statements(quoted):
        op.execute(statement)
    seed = taxonomy.seed_rows(taxonomy.packaged(TAXONOMY_VERSION))
    for name in SEED_ORDER:
        op.bulk_insert(metadata.tables[f"{schema_0002.SCHEMA}.{name}"], seed[name])
    org_id = str(uuid7())
    op.bulk_insert(metadata.tables[f"{schema_0002.SCHEMA}.organization"], [{"id": org_id, **DEFAULT_ORGANIZATION}])
    op.bulk_insert(metadata.tables[f"{schema_0002.SCHEMA}.org_revision"], [{"org_id": org_id, "last": 0}])


def downgrade() -> None:
    bind = op.get_bind()
    metadata = _tables()
    metadata.drop_all(bind)
    op.execute(f"DROP FUNCTION {schema_0002.SCHEMA}.set_once_revision_to()")
    op.execute(f"DROP FUNCTION {schema_0002.SCHEMA}.change_set_server_fields()")
