"""Import planning (ADR-0008 §8, ADR-0003): the difference between an import batch and the current graph.

Matching is by exact identity only: a provider or source key, an organization key, or a reference to another item of
the same batch; never by name. An item that cannot become a canonical fact is **rejected and reported** with its
rule and reasons (U-5: the import report), never stored or silently dropped. An empty plan means re-importing the batch
changes nothing, so no change set is written. Same semantics as atlas-specs ``tools/canonical_model.plan_batch``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlas.domain.canonical.graph import Row, State, check_pair, discriminator_of
from atlas.domain.canonical.identity import NormalizationError, normalize
from atlas.domain.canonical.taxonomy import Taxonomy

ENTITY_COMPARE = (
    "type_key",
    "display_name",
    "lifecycle",
    "provider",
    "native_type",
    "environment_scope",
    "environment_id",
    "container_id",
    "attributes",
)


@dataclass
class Plan:
    new_entities: list[Row] = field(default_factory=list)
    changed_entities: list[tuple[str, Row]] = field(default_factory=list)
    new_relationships: list[Row] = field(default_factory=list)
    new_containments: list[tuple[str, str]] = field(default_factory=list)  # (parent, child) implied by "parent"
    changed_relationships: list[tuple[str, Row]] = field(default_factory=list)
    rejected: list[Row] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.new_entities or self.changed_entities or self.new_relationships or self.changed_relationships)

    def report(self) -> list[Row]:
        """The import report's rejected items (U-5): reference, rule and reasons."""
        return [{"ref": r["item"].get("ref"), "rule": r["rule"], "reasons": r["reasons"]} for r in self.rejected]


def plan_batch(tax: Taxonomy, batch: Row, st: State) -> Plan:
    plan = Plan()
    ev_kinds = {e["ref"]: e["kind"] for e in batch["evidence"]}
    by_ref: dict[str, Row] = {}

    def resolve(ref: Row | None) -> str | None:
        if ref is None:
            return None
        if "ref" in ref:
            return by_ref[ref["ref"]]["entity_id"] if ref["ref"] in by_ref else None
        if "org_key" in ref:
            return st.identities.get(("org.key", ref["org_key"].strip().lower()))
        ns, original = ref["identity"]["namespace"], ref["identity"]["original_key"]
        try:
            key = normalize(tax, ns, original).normalized_key
        except NormalizationError:
            return None
        return st.identities.get((ns, key))

    def reject(item: Row, rule: str, reasons: list[str]) -> None:
        plan.rejected.append({"item": item, "rule": rule, "reasons": reasons})

    for item in batch["entities"]:
        _plan_entity(tax, item, st, ev_kinds, by_ref, resolve, reject, plan)
    for item in batch["relationships"]:
        _plan_relationship(tax, item, st, ev_kinds, by_ref, resolve, reject, plan)
    return plan


def _plan_entity(
    tax: Taxonomy,
    item: Row,
    st: State,
    ev_kinds: dict[str, str],
    by_ref: dict[str, Row],
    resolve: Any,
    reject: Any,
    plan: Plan,
) -> None:
    t = tax.types.get(item["type_key"])
    if t is None:
        reject(item, "D-01", [f"unknown type {item['type_key']}"])
        return
    idents, bad = [], []
    for ident in item["identities"]:
        try:
            key = normalize(tax, ident["namespace"], ident["original_key"])
        except NormalizationError as exc:
            bad.append(str(exc))
            continue
        idents.append(
            {
                "namespace": ident["namespace"],
                "original_key": ident["original_key"],
                "normalized_key": key.normalized_key,
                "key_parts": key.key_parts,
                "normalization_version": key.normalization_version,
            }
        )
    if bad:
        reject(item, "H-08", bad)
        return
    bound = {st.identities.get((i["namespace"], i["normalized_key"])) for i in idents} - {None}
    if len(bound) > 1:
        reject(item, "D-17", ["identities resolve to different entities: ambiguous, no link (ADR-0003)"])
        return
    missing = [a for a in t["required_attributes"] if a not in (item.get("attributes") or {})]
    if missing:
        reject(item, "D-01", [f"missing required attributes {missing}"])
        return
    eid = next(iter(bound)) if bound else item.get("id")
    if eid is None:
        reject(
            item, "D-18", ["no identity match and no explicit fixture ID; the loader assigns IDs only outside fixtures"]
        )
        return
    desired = {
        "entity_id": eid,
        "type_key": item["type_key"],
        "category": t["category"],
        "display_name": item["display_name"],
        "lifecycle": item.get("lifecycle", "active"),
        "provider": item.get("provider"),
        "native_type": item.get("native_type"),
        "environment_scope": item.get("environment_scope"),
        "environment_id": resolve(item.get("environment")),
        "container_id": resolve(item.get("parent")),
        "attributes": item.get("attributes") or {},
        "identities": idents,
        "evidence_kinds": sorted({ev_kinds[r] for r in item["evidence_refs"]}),
        "evidence_refs": item["evidence_refs"],
        "ref": item["ref"],
    }
    by_ref[item["ref"]] = desired
    current = st.entities.get(eid)
    if current is None:
        plan.new_entities.append(desired)
        if desired["container_id"]:
            plan.new_containments.append((desired["container_id"], eid))
        return
    changes = {k: desired[k] for k in ENTITY_COMPARE if current[k] != desired[k]}
    new_ids = [i for i in idents if (i["namespace"], i["normalized_key"]) not in st.identities]
    if changes or new_ids:
        plan.changed_entities.append(
            (eid, {"fields": changes, "identities": new_ids, "evidence_refs": item["evidence_refs"]})
        )


def _plan_relationship(
    tax: Taxonomy,
    item: Row,
    st: State,
    ev_kinds: dict[str, str],
    by_ref: dict[str, Row],
    resolve: Any,
    reject: Any,
    plan: Plan,
) -> None:
    fid, tid = resolve(item["from"]), resolve(item["to"])

    def version_of(eid: str | None) -> Row | None:
        if eid is None:
            return None
        return next((d for d in by_ref.values() if d["entity_id"] == eid), None) or st.entities.get(eid)

    fv, tv = version_of(fid), version_of(tid)
    attrs = item.get("attributes") or {}
    if fv is None or tv is None:
        reject(item, "H-04", ["an endpoint does not resolve (rejected entity, or no identity match)"])
        return
    reasons = check_pair(tax, item["rel_type"], fv, tv, attrs)
    kinds = {ev_kinds[r] for r in item["evidence_refs"]}
    rule = tax.rels.get(item["rel_type"])
    if rule and not kinds <= set(rule["allowed_evidence_kinds"]):
        reasons.append(
            f"evidence kinds {sorted(kinds - set(rule['allowed_evidence_kinds']))} not allowed for {item['rel_type']}"
        )
    if reasons:
        reject(item, "D-04", reasons)
        return
    disc = discriminator_of(tax, item["rel_type"], attrs)
    existing = {
        (r["rel_type"], r["from_id"], r["to_id"], r["discriminator"]): rid for rid, r in st.relationships.items()
    }
    rid = existing.get((item["rel_type"], fid, tid, disc))
    if rid is not None:
        if st.relationships[rid]["version"]["attributes"] != attrs:
            plan.changed_relationships.append((rid, {"attributes": attrs, "evidence_refs": item["evidence_refs"]}))
        return
    if item.get("id") is None:
        reject(item, "D-18", ["new relationship without an explicit fixture ID"])
        return
    plan.new_relationships.append(
        {
            "id": item["id"],
            "rel_type": item["rel_type"],
            "from_id": fid,
            "to_id": tid,
            "discriminator": disc,
            "attributes": attrs,
            "evidence_refs": item["evidence_refs"],
            "evidence_kinds": sorted(kinds),
        }
    )
