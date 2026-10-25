"""Versioned canonical taxonomy (ADR-0017, ADR-0019).

The approved artifact is packaged under ``data/taxonomy-<version>.json``: a verbatim copy of atlas-specs
``contracts/taxonomy/atlas-taxonomy.v1.json`` at the ``specs.lock`` commit (a pinned-specs test proves the copy is
byte-identical). Migrations seed the reference tables from it (:func:`seed_rows`).
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

CURRENT_VERSION = "1.1.0"
SYNTHETIC_STATUS = "synthetic_test_only"


class Taxonomy:
    """One taxonomy version, optionally an overlay over a base version."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.version: str = doc["taxonomy_version"]
        self.status: str = doc["status"]
        self.enums: dict[str, list[str]] = doc["enumerations"]
        self.types: dict[str, dict[str, Any]] = {t["type_key"]: t for t in doc["entity_types"]}
        self.rels: dict[str, dict[str, Any]] = {r["rel_type"]: r for r in doc["relationship_types"]}
        self.categories: set[str] = {c["category"] for c in doc["categories"]}
        self.providers: dict[str, dict[str, Any]] = {p["provider"]: p for p in doc["providers"]}
        self.namespaces: dict[str, dict[str, Any]] = {n["namespace"]: n for n in doc["identity_namespaces"]}
        self.actor_namespaces: dict[str, dict[str, Any]] = {a["namespace"]: a for a in doc["actor_namespaces"]}
        self.scope_rules: dict[str, dict[str, Any]] = doc["provider_scope_rules"]
        self.mappings: dict[str, dict[str, str]] = {
            prov: {m["native_type"]: m["category"] for m in items}
            for prov, items in doc["native_type_mappings"].items()
        }
        common = doc["common_attributes"]
        self.attr_name_re = re.compile(common["attribute_name_pattern"])
        self.forbidden_attr_re = re.compile(common["forbidden_attribute_name_pattern"])
        self.platform_providers: set[str] = set(common["platform_required_for_providers"])

    def with_overlay(self, overlay: dict[str, Any]) -> Taxonomy:
        """A synthetic, test-only delta over this version (atlas-specs ``taxonomy-overlay`` schema)."""
        if overlay.get("extends") != self.version:
            raise ValueError(f"overlay extends {overlay.get('extends')}, base is {self.version}")
        doc = copy.deepcopy(self.doc)
        doc["taxonomy_version"] = overlay["taxonomy_version"]
        doc["status"] = overlay["status"]
        for prov, items in overlay.get("add_native_type_mappings", {}).items():
            doc["native_type_mappings"].setdefault(prov, []).extend(items)
        return Taxonomy(doc)

    def enum_values(self, spec: Any) -> list[str]:
        """An attribute vocabulary: a list, or ``@name`` for a taxonomy enumeration."""
        if isinstance(spec, str) and spec.startswith("@"):
            return self.enums[spec[1:]]
        return list(spec)

    def matches(self, type_key: str, refs: list[str]) -> bool:
        """True if ``type_key`` matches a type key, ``category:X``, ``category:physical`` or ``*``."""
        t = self.types.get(type_key)
        if t is None:
            return False
        for ref in refs:
            if ref in ("*", type_key):
                return True
            if ref == "category:physical" and t["kind"] == "physical":
                return True
            if ref.startswith("category:") and t["category"] == ref.split(":", 1)[1]:
                return True
        return False

    def allowed_parent(self, child_type: str, parent_type: str) -> bool:
        t = self.types.get(child_type)
        return t is not None and self.matches(parent_type, t["containment_parents"])

    def is_physical(self, type_key: str) -> bool:
        return self.types.get(type_key, {}).get("kind") == "physical"


@lru_cache(maxsize=4)
def artifact_text(version: str = CURRENT_VERSION) -> str:
    """The packaged artifact of one approved version (``data/taxonomy-<version>.json``)."""
    return resources.files("atlas.domain.canonical").joinpath("data", f"taxonomy-{version}.json").read_text("utf-8")


@lru_cache(maxsize=4)
def packaged(version: str) -> Taxonomy:
    """An approved, packaged version. Migrations name their version explicitly, so they never change later."""
    taxonomy = Taxonomy(json.loads(artifact_text(version)))
    if taxonomy.version != version:
        raise RuntimeError(f"packaged taxonomy file for {version} holds {taxonomy.version}")
    return taxonomy


def current() -> Taxonomy:
    """The approved taxonomy this backend validates new facts against."""
    return packaged(CURRENT_VERSION)


# --------------------------------------------------------------------------- reference-table seed (ADR-0001)


def _pairs(tax: Taxonomy, rel: dict[str, Any]) -> list[tuple[str, str]]:
    """Concrete (from_type, to_type) pairs a relationship type allows, expanded from its type/category references."""
    out: set[tuple[str, str]] = set()
    if rel["rel_type"] == "CONTAINS":
        for child, t in tax.types.items():
            for parent in tax.types:
                if tax.matches(parent, t["containment_parents"]):
                    out.add((parent, child))
        return sorted(out)
    for pair in rel["pairs"]:
        for frm in tax.types:
            if not tax.matches(frm, pair["from"]):
                continue
            for to in tax.types:
                if tax.matches(to, pair["to"]) and to not in pair.get("to_exclude", []):
                    out.add((frm, to))
    return sorted(out)


def seed_rows(tax: Taxonomy) -> dict[str, list[dict[str, Any]]]:
    """Rows of every taxonomy reference table, deterministically ordered. CI compares the database with this."""
    v = tax.version
    attribute_rows: list[dict[str, Any]] = []
    for t in tax.doc["entity_types"]:
        for name in sorted(set(t["required_attributes"]) | set(t["attribute_enums"])):
            spec = t["attribute_enums"].get(name)
            attribute_rows.append(
                {
                    "owner_kind": "entity_type",
                    "owner_key": t["type_key"],
                    "attribute": name,
                    "required": name in t["required_attributes"],
                    "allowed_values": tax.enum_values(spec) if spec is not None else None,
                    "taxonomy_version": v,
                }
            )
    for r in tax.doc["relationship_types"]:
        for name in sorted(set(r["required_attributes"]) | set(r["optional_attributes"])):
            spec = r["attribute_enums"].get(name)
            attribute_rows.append(
                {
                    "owner_kind": "relationship_type",
                    "owner_key": r["rel_type"],
                    "attribute": name,
                    "required": name in r["required_attributes"],
                    "allowed_values": tax.enum_values(spec) if spec is not None else None,
                    "taxonomy_version": v,
                }
            )
    return {
        "taxonomy_release": [{"version": v, "status": tax.status}],
        "provider": [
            {
                "key": p["provider"],
                "display_name": p["display_name"],
                "provider_kind": p["provider_kind"],
                "requires_platform": p["requires_platform"],
                "taxonomy_version": v,
            }
            for p in tax.doc["providers"]
        ],
        "category": [
            {"key": c["category"], "description": c["description"], "taxonomy_version": v}
            for c in tax.doc["categories"]
        ],
        "identity_namespace": [
            {
                "namespace": n["namespace"],
                "provider": n["provider"],
                "status": n["status"],
                "normalization_rule": n["normalization_rule"],
                "case_rule": n["case_rule"],
                "taxonomy_version": v,
            }
            for n in tax.doc["identity_namespaces"]
        ],
        "native_type_mapping": sorted(
            (
                {"provider": prov, "native_type": nt, "category": cat, "taxonomy_version": v}
                for prov, items in tax.mappings.items()
                for nt, cat in items.items()
            ),
            key=lambda r: (r["provider"], r["native_type"]),
        ),
        "actor_namespace": [
            {
                "namespace": a["namespace"],
                "actor_kind": a["actor_kind"],
                "key_pattern": a["pattern"],
                "taxonomy_version": v,
            }
            for a in tax.doc["actor_namespaces"]
        ],
        "entity_type": [
            {
                "type_key": t["type_key"],
                "kind": t["kind"],
                "category": t["category"],
                "provider": t["provider"],
                "environment_scope": t["environment_scope"],
                "native_type": t.get("native_type"),
                "taxonomy_version": v,
            }
            for t in tax.doc["entity_types"]
        ],
        "relationship_type": [
            {
                "rel_type": r["rel_type"],
                "status": r["status"],
                "cardinality": r["cardinality"],
                "discriminator": r["discriminator"],
                "cross_environment_allowed": r["cross_environment_allowed"],
                "logical_only": r["logical_only"],
                "taxonomy_version": v,
            }
            for r in tax.doc["relationship_types"]
        ],
        "relationship_evidence_kind": [
            {"rel_type": r["rel_type"], "evidence_kind": k, "taxonomy_version": v}
            for r in tax.doc["relationship_types"]
            for k in sorted(r["allowed_evidence_kinds"])
        ],
        "relationship_rule": [
            {"rel_type": r["rel_type"], "from_type": f, "to_type": t, "taxonomy_version": v}
            for r in tax.doc["relationship_types"]
            if r["status"] == "active"
            for f, t in _pairs(tax, r)
        ],
        "attribute_rule": attribute_rows,
    }
