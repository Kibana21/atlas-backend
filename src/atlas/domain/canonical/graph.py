"""Canonical rows, as-of reconstruction, graph hash and relationship compatibility (ADR-0008, ADR-0017).

Rows are dictionaries in the canonical schemas' JSON shapes (IDs as strings, timestamps as RFC 3339 ``Z``).
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any

from atlas.domain.canonical.taxonomy import Taxonomy
from atlas.domain.canonical.trust import NO_POLICY, canonical_json

Row = dict[str, Any]
TABLES = (
    "organizations",
    "change_sets",
    "entities",
    "entity_versions",
    "relationships",
    "relationship_versions",
    "external_identities",
    "evidence",
    "fact_assertions",
    "graph_snapshots",
)


def sha256_of(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def valid_at(row: Row, revision: int) -> bool:
    return bool(row["revision_from"] <= revision and (row["revision_to"] is None or row["revision_to"] > revision))


@dataclass
class Dataset:
    """Canonical rows of one or more organizations, keyed by the fixture's table names."""

    tables: dict[str, list[Row]]
    policy: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(NO_POLICY))

    def __post_init__(self) -> None:
        for name in TABLES:
            self.tables.setdefault(name, [])

    def org_ids(self) -> list[str]:
        return [o["id"] for o in self.tables["organizations"]]

    def head(self, org_id: str) -> int:
        return max((c["revision"] for c in self.tables["change_sets"] if c["org_id"] == org_id), default=0)


@dataclass
class State:
    """Graph of one organization as of one revision (ADR-0008 §6): present facts and current assertions."""

    org_id: str
    revision: int
    entities: dict[str, Row] = field(default_factory=dict)
    relationships: dict[str, Row] = field(default_factory=dict)  # identity fields + "version"
    identities: dict[tuple[str, str], str] = field(default_factory=dict)  # (namespace, normalized key) -> entity
    assertions: list[Row] = field(default_factory=list)


def as_of(ds: Dataset, org_id: str, revision: int) -> State:
    st = State(org_id, revision)
    for v in ds.tables["entity_versions"]:
        if v["org_id"] == org_id and valid_at(v, revision) and v["record_state"] == "present":
            st.entities[v["entity_id"]] = v
    identity = {r["id"]: r for r in ds.tables["relationships"] if r["org_id"] == org_id}
    for v in ds.tables["relationship_versions"]:
        if v["org_id"] == org_id and valid_at(v, revision) and v["record_state"] == "present":
            st.relationships[v["relationship_id"]] = {**identity[v["relationship_id"]], "version": v}
    for i in ds.tables["external_identities"]:
        if i["org_id"] == org_id and valid_at(i, revision):
            st.identities[(i["namespace"], i["normalized_key"])] = i["entity_id"]
    st.assertions = [a for a in ds.tables["fact_assertions"] if a["org_id"] == org_id and valid_at(a, revision)]
    return st


ENTITY_HASH_FIELDS = (
    "type_key",
    "category",
    "provider",
    "native_type",
    "merged_into",
    "display_name",
    "lifecycle",
    "environment_scope",
    "environment_id",
    "container_id",
    "attributes",
    "trust_state",
    "verification_state",
)
REL_HASH_FIELDS = (
    "lifecycle",
    "environment_id",
    "cross_environment",
    "shared_endpoint",
    "cross_provider",
    "attributes",
    "trust_state",
    "verification_state",
)


def graph_hash(st: State) -> str:
    """SHA-256 over present facts and their current assertions; no revisions or timestamps (ADR-0008 §4)."""
    ents = [{"id": i, **{k: v[k] for k in ENTITY_HASH_FIELDS}} for i, v in sorted(st.entities.items())]
    rels = [
        {
            "id": i,
            "rel_type": r["rel_type"],
            "from_id": r["from_id"],
            "to_id": r["to_id"],
            "discriminator": r["discriminator"],
            **{k: r["version"][k] for k in REL_HASH_FIELDS},
        }
        for i, r in sorted(st.relationships.items())
    ]
    present = set(st.entities) | set(st.relationships)
    asserts = sorted(
        canonical_json([a["fact_id"], a["field_path"], a["asserted_value"], a["evidence_id"]])
        for a in st.assertions
        if a["fact_id"] in present
    )
    return sha256_of({"entities": ents, "relationships": rels, "assertions": asserts})


def relationship_flags(frm: Row, to: Row) -> dict[str, Any]:
    """Environment and provider flags derived from the endpoints (ADR-0017 §4.4, ADR-0019 §6, interpretation I-1)."""
    single = [v for v in (frm, to) if v["environment_scope"] == "single"]
    envs = {v["environment_id"] for v in single}
    cross_env = len(single) == 2 and len(envs) == 2
    env_id = None if cross_env or not single else next(iter(envs))
    return {
        "environment_id": env_id,
        "cross_environment": cross_env,
        "shared_endpoint": any(v["environment_scope"] == "shared" for v in (frm, to)),
        "cross_provider": bool(frm["provider"] and to["provider"] and frm["provider"] != to["provider"]),
    }


def discriminator_of(tax: Taxonomy, rel_type: str, attributes: Row) -> str:
    name = tax.rels[rel_type]["discriminator"] if rel_type in tax.rels else None
    return str(attributes.get(name, "")) if name else ""


def check_pair(tax: Taxonomy, rel_type: str, frm: Row, to: Row, attributes: Row) -> list[str]:
    """Why (type, endpoints, attributes) is not allowed by the matrix (D-04). Empty means allowed."""
    rel = tax.rels.get(rel_type)
    if rel is None:
        return [f"unknown relationship type {rel_type}"]
    if rel["status"] != "active":
        return [f"{rel_type} is reserved in taxonomy {tax.version}; writes are rejected"]
    ft, tt = frm["type_key"], to["type_key"]
    if ft not in tax.types or tt not in tax.types:
        return [f"unknown endpoint type {ft if ft not in tax.types else tt}"]
    if rel_type == "CONTAINS":
        reasons = []
        if not tax.allowed_parent(tt, ft):
            reasons.append(f"{ft} is not an allowed CONTAINS parent of {tt}")
        if tax.types[ft]["kind"] != tax.types[tt]["kind"]:
            reasons.append("containment never crosses logical and physical families")
        elif tax.types[ft]["kind"] == "physical" and frm["provider"] != to["provider"]:
            reasons.append("physical containment stays within one provider")
        return reasons
    reasons = []
    allowed = False
    for pair in rel["pairs"]:
        if not (tax.matches(ft, pair["from"]) and tax.matches(tt, pair["to"]) and tt not in pair.get("to_exclude", [])):
            continue
        cond = pair.get("when_from")
        if cond and any(frm.get(k) != v for k, v in cond.items()):
            continue
        kinds = pair.get("connectivity_kinds")
        if kinds and attributes.get("connectivity_kind") not in kinds:
            continue
        allowed = True
        break
    if not allowed:
        suffix = f" with connectivity_kind {attributes.get('connectivity_kind')}" if rel_type == "CONNECTS_TO" else ""
        reasons.append(f"{rel_type} is not allowed from {ft} to {tt}{suffix}")
    for name in rel["required_attributes"]:
        if attributes.get(name) in (None, ""):
            reasons.append(f"{rel_type} requires attribute {name}")
    known = set(rel["required_attributes"]) | set(rel["optional_attributes"])
    reasons += [f"{rel_type} has no attribute {name}" for name in attributes if name not in known]
    for name, spec in rel["attribute_enums"].items():
        if name in attributes and attributes[name] not in tax.enum_values(spec):
            reasons.append(f"{rel_type}.{name} = {attributes[name]!r} is not in the vocabulary")
    if rel["logical_only"] and not (tax.types[ft]["kind"] == tax.types[tt]["kind"] == "logical"):
        reasons.append(f"{rel_type} is between logical types only")
    return reasons
