"""Mapping between canonical JSON rows (atlas-specs schemas, golden fixture) and database columns.

Differences from the JSON shape: actors and verifiers are split into ``*_namespace``/``*_key`` columns (so the
namespace is a foreign key), and ``fact_assertion`` carries ``entity_fact_id``/``relationship_fact_id``/``rel_type``
so that its fact, organization and allowed evidence kind are enforced by foreign keys.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

Row = dict[str, Any]

COLUMNS: dict[str, tuple[str, ...]] = {
    "organization": ("id", "key", "display_name"),
    "change_set": (
        "org_id",
        "revision",
        "kind",
        "channel",
        "actor_kind",
        "actor_namespace",
        "actor_key",
        "actor_label",
        "source_ref",
        "input_hash",
        "reverts_revision",
        "reason",
        "change_reference",
        "created_at",
        "db_role",
    ),
    "entity": ("org_id", "id", "created_revision"),
    "entity_version": (
        "org_id",
        "entity_id",
        "revision_from",
        "revision_to",
        "type_key",
        "category",
        "provider",
        "native_type",
        "record_state",
        "merged_into",
        "display_name",
        "lifecycle",
        "environment_scope",
        "environment_id",
        "container_id",
        "attributes",
        "trust_state",
        "verification_state",
        "verified_by_namespace",
        "verified_by_key",
        "verified_at",
        "expires_at",
        "taxonomy_version",
    ),
    "relationship": ("org_id", "id", "rel_type", "from_id", "to_id", "discriminator", "created_revision"),
    "relationship_version": (
        "org_id",
        "relationship_id",
        "revision_from",
        "revision_to",
        "record_state",
        "lifecycle",
        "environment_id",
        "cross_environment",
        "shared_endpoint",
        "cross_provider",
        "attributes",
        "trust_state",
        "verification_state",
        "verified_by_namespace",
        "verified_by_key",
        "verified_at",
        "expires_at",
        "taxonomy_version",
    ),
    "external_identity": (
        "org_id",
        "id",
        "entity_id",
        "namespace",
        "original_key",
        "normalized_key",
        "normalization_version",
        "key_parts",
        "match_method",
        "revision_from",
        "revision_to",
    ),
    "evidence": (
        "org_id",
        "id",
        "kind",
        "source_ref",
        "artifact_ref",
        "artifact_hash",
        "method",
        "confidence",
        "observed_at",
        "valid_from",
        "valid_to",
        "change_revision",
    ),
    "fact_assertion": (
        "org_id",
        "id",
        "fact_kind",
        "fact_id",
        "entity_fact_id",
        "relationship_fact_id",
        "rel_type",
        "field_path",
        "asserted_value",
        "evidence_id",
        "evidence_kind",
        "revision_from",
        "revision_to",
    ),
    "graph_snapshot": ("org_id", "id", "revision", "label", "status", "graph_hash"),
}
FIXTURE_TABLE = {
    "organizations": "organization",
    "change_sets": "change_set",
    "entities": "entity",
    "entity_versions": "entity_version",
    "relationships": "relationship",
    "relationship_versions": "relationship_version",
    "external_identities": "external_identity",
    "evidence": "evidence",
    "fact_assertions": "fact_assertion",
    "graph_snapshots": "graph_snapshot",
}
TIMESTAMPS = frozenset({"created_at", "verified_at", "expires_at", "observed_at", "valid_from", "valid_to"})
JSON_COLUMNS = frozenset({"attributes", "key_parts"})
UUIDS = frozenset(
    {
        "id",
        "org_id",
        "entity_id",
        "relationship_id",
        "merged_into",
        "environment_id",
        "container_id",
        "from_id",
        "to_id",
        "fact_id",
        "entity_fact_id",
        "relationship_fact_id",
        "evidence_id",
    }
)


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_ts(value: datetime | None) -> str | None:
    """RFC 3339 UTC with ``Z``; microseconds only when present (the canonical schemas' timestamp format)."""
    if value is None:
        return None
    utc = value.astimezone(UTC)
    fraction = f".{utc.microsecond:06d}" if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def _split_ref(ref: dict[str, Any] | None) -> tuple[str | None, str | None]:
    return (None, None) if ref is None else (ref["namespace"], ref["key"])


def to_db(table: str, row: Row, *, rel_types: dict[str, str] | None = None) -> tuple[Any, ...]:
    """Parameters for ``INSERT INTO <table> (COLUMNS[table])``. ``rel_types`` maps relationship IDs to their type."""
    r = dict(row)
    if table == "change_set":
        actor = r.pop("actor")
        r.update(
            actor_kind=actor["kind"],
            actor_namespace=actor["namespace"],
            actor_key=actor["key"],
            actor_label=actor.get("label"),
        )
    if table in ("entity_version", "relationship_version"):
        r["verified_by_namespace"], r["verified_by_key"] = _split_ref(r.pop("verified_by"))
    if table == "fact_assertion":
        entity = r["fact_kind"] == "entity"
        r["entity_fact_id"] = r["fact_id"] if entity else None
        r["relationship_fact_id"] = None if entity else r["fact_id"]
        r["rel_type"] = None if entity else (rel_types or {})[r["fact_id"]]
    out: list[Any] = []
    for column in COLUMNS[table]:
        value = r.get(column)
        if column in TIMESTAMPS:
            value = parse_ts(value)
        elif column in JSON_COLUMNS:
            value = Jsonb(value)
        elif column == "asserted_value":
            value = None if r["field_path"] is None else Jsonb(value)
        out.append(value)
    return tuple(out)


def from_db(table: str, record: Row) -> Row:
    """A canonical JSON row from a database record (``COLUMNS[table]`` keys)."""
    r: Row = {}
    for column, value in record.items():
        if column in TIMESTAMPS:
            r[column] = format_ts(value)
        elif column in UUIDS and value is not None:
            r[column] = str(value)
        else:
            r[column] = value
    if table == "change_set":
        r["actor"] = {
            "kind": r.pop("actor_kind"),
            "namespace": r.pop("actor_namespace"),
            "key": r.pop("actor_key"),
            "label": r.pop("actor_label"),
        }
    if table in ("entity_version", "relationship_version"):
        namespace, key = r.pop("verified_by_namespace"), r.pop("verified_by_key")
        r["verified_by"] = None if namespace is None else {"namespace": namespace, "key": key}
    if table == "fact_assertion":
        for column in ("entity_fact_id", "relationship_fact_id", "rel_type"):
            r.pop(column)
    return r


def insert_sql(table: str) -> str:
    columns = COLUMNS[table]
    return (
        f"INSERT INTO atlas.{table} ({', '.join(columns)}) "  # noqa: S608 - constant identifiers
        f"VALUES ({', '.join(['%s'] * len(columns))})"
    )
