"""Validation rules (atlas-specs ``contracts/canonical-validation-rules.md``): HARD, DOMAIN and ADVISORY.

:func:`validate` checks canonical rows. With ``scope`` it checks only the given revisions of each organization: the
publishing procedure validates the one revision it is about to write against the current state. Without it, every
revision is checked, including history rules (gap-free revisions, contiguous versions), as for the golden fixture.
HARD rules are also database constraints; here they give readable reasons before a write is attempted.
ADVISORY findings never block (:func:`advisory_findings`).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from atlas.domain.canonical.graph import (
    Dataset,
    Row,
    as_of,
    check_pair,
    discriminator_of,
    relationship_flags,
    sha256_of,
    valid_at,
)
from atlas.domain.canonical.identity import NormalizationError, normalize
from atlas.domain.canonical.taxonomy import Taxonomy
from atlas.domain.canonical.trust import Claim, evaluate

HARD, DOMAIN, ADVISORY = "HARD", "DOMAIN", "ADVISORY"
SERVICE_CHANNELS = frozenset({"fixture_cli", "import", "connector"})
HUMAN_KINDS = frozenset({"steward_edit", "merge", "split"})
REVISION_FIELDS = ("revision_from", "revision_to", "taxonomy_version")


@dataclass(frozen=True)
class Finding:
    rule: str
    cls: str
    message: str

    def __str__(self) -> str:
        return f"[{self.cls}] {self.rule}: {self.message}"


class Index:
    def __init__(self, ds: Dataset) -> None:
        t = ds.tables
        self.entity_org = {e["id"]: e["org_id"] for e in t["entities"]}
        self.rel = {r["id"]: r for r in t["relationships"]}
        self.ev = {e["id"]: e for e in t["evidence"]}
        self.ev_versions: dict[str, list[Row]] = defaultdict(list)
        for v in t["entity_versions"]:
            self.ev_versions[v["entity_id"]].append(v)
        self.rv_versions: dict[str, list[Row]] = defaultdict(list)
        for v in t["relationship_versions"]:
            self.rv_versions[v["relationship_id"]].append(v)
        for versions in (self.ev_versions, self.rv_versions):
            for vs in versions.values():
                vs.sort(key=lambda v: v["revision_from"])
        self.cs = {(c["org_id"], c["revision"]): c for c in t["change_sets"]}
        self.heads = {o["id"]: ds.head(o["id"]) for o in t["organizations"]}
        self.asserts: dict[str, list[Row]] = defaultdict(list)
        for a in t["fact_assertions"]:
            self.asserts[a["fact_id"]].append(a)
        self.identities: dict[str, list[Row]] = defaultdict(list)
        for i in t["external_identities"]:
            self.identities[i["entity_id"]].append(i)

    def entity_at(self, eid: str, rev: int) -> Row | None:
        return next((v for v in self.ev_versions.get(eid, []) if valid_at(v, rev)), None)


class Scope:
    """Which revisions to check: every revision (``None``), or a set per organization."""

    def __init__(self, ix: Index, revisions: dict[str, set[int]] | None) -> None:
        self.full = revisions is None
        self.revisions = {o: set(range(1, h + 1)) for o, h in ix.heads.items()} if revisions is None else revisions

    def has(self, org: str, rev: int | None) -> bool:
        return rev is not None and rev in self.revisions.get(org, set())


def taxonomy_for(taxes: dict[str, Taxonomy], version: str, label: str, f: list[Finding]) -> Taxonomy | None:
    tax = taxes.get(version)
    if tax is None:
        f.append(Finding("D-01", DOMAIN, f"{label}: unknown taxonomy version {version}"))
    return tax


def base_taxonomy(taxes: dict[str, Taxonomy]) -> Taxonomy:
    """The approved version among those loaded (overlays are synthetic test-only deltas)."""
    return next(t for t in taxes.values() if t.status != "synthetic_test_only")


def validate(ds: Dataset, taxes: dict[str, Taxonomy], scope: dict[str, set[int]] | None = None) -> list[Finding]:
    """HARD and DOMAIN findings. An empty list means the rows may be published."""
    f: list[Finding] = []
    ix = Index(ds)
    sc = Scope(ix, scope)
    _references(ds, ix, sc, f)
    _revisions(ds, ix, sc, f)
    _identities(ds, ix, sc, taxes, f)
    _entities(ds, ix, sc, taxes, f)
    _relationships(ds, ix, sc, taxes, f)
    _per_revision(ds, ix, sc, f)
    _change_sets(ds, ix, sc, taxes, f)
    _trust(ds, ix, sc, taxes, f)
    return f


# --------------------------------------------------------------------------- HARD


def _references(ds: Dataset, ix: Index, sc: Scope, f: list[Finding]) -> None:
    t = ds.tables
    orgs = set(ds.org_ids())
    if sc.full:
        for table in (
            "organizations",
            "entities",
            "relationships",
            "evidence",
            "fact_assertions",
            "external_identities",
            "graph_snapshots",
        ):
            ids = [r["id"] for r in t[table]]
            if len(ids) != len(set(ids)):
                f.append(Finding("H-02", HARD, f"{table}: duplicate IDs"))

    def same_org(label: str, org: str, ref: str | None, where: dict[str, str]) -> None:
        if ref is None:
            return
        if ref not in where:
            f.append(Finding("H-04", HARD, f"{label}: reference {ref} does not resolve"))
        elif where[ref] != org:
            f.append(Finding("H-03", HARD, f"{label}: reference {ref} crosses organizations"))

    rel_org = {r["id"]: r["org_id"] for r in t["relationships"]}
    ev_org = {e["id"]: e["org_id"] for e in t["evidence"]}
    for table in ("entity_versions", "relationship_versions", "external_identities", "fact_assertions"):
        for r in t[table]:
            if r["org_id"] not in orgs:
                f.append(Finding("H-04", HARD, f"{table}: unknown organization {r['org_id']}"))
    for v in t["entity_versions"]:
        if sc.has(v["org_id"], v["revision_from"]):
            lbl = f"entity_version {v['entity_id']}@{v['revision_from']}"
            for fld in ("entity_id", "merged_into", "environment_id", "container_id"):
                same_org(f"{lbl}.{fld}", v["org_id"], v[fld], ix.entity_org)
    for r in t["relationships"]:
        if sc.has(r["org_id"], r["created_revision"]):
            for fld in ("from_id", "to_id"):
                same_org(f"relationship {r['id']}.{fld}", r["org_id"], r[fld], ix.entity_org)
            if r["from_id"] == r["to_id"]:
                f.append(Finding("H-09", HARD, f"relationship {r['id']}: self-relationship"))
    for v in t["relationship_versions"]:
        if sc.has(v["org_id"], v["revision_from"]):
            same_org(f"relationship_version {v['relationship_id']}", v["org_id"], v["relationship_id"], rel_org)
            same_org(
                f"relationship_version {v['relationship_id']}.environment_id",
                v["org_id"],
                v["environment_id"],
                ix.entity_org,
            )
    for i in t["external_identities"]:
        if sc.has(i["org_id"], i["revision_from"]):
            same_org(f"identity {i['id']}", i["org_id"], i["entity_id"], ix.entity_org)
    for a in t["fact_assertions"]:
        if not sc.has(a["org_id"], a["revision_from"]):
            continue
        same_org(f"assertion {a['id']}.evidence_id", a["org_id"], a["evidence_id"], ev_org)
        targets = ix.entity_org if a["fact_kind"] == "entity" else rel_org
        same_org(f"assertion {a['id']}.fact_id", a["org_id"], a["fact_id"], targets)
        ev = ix.ev.get(a["evidence_id"])
        if ev and ev["kind"] != a["evidence_kind"]:
            f.append(Finding("H-12", HARD, f"assertion {a['id']}: evidence_kind differs from its evidence"))
        if ev and ev["change_revision"] > a["revision_from"]:
            f.append(Finding("H-12", HARD, f"assertion {a['id']}: evidence recorded after the assertion"))


def _revisions(ds: Dataset, ix: Index, sc: Scope, f: list[Finding]) -> None:
    t = ds.tables
    if sc.full:
        by_org: dict[str, list[int]] = defaultdict(list)
        for c in t["change_sets"]:
            by_org[c["org_id"]].append(c["revision"])
        for org, revs in by_org.items():
            if sorted(revs) != list(range(1, len(revs) + 1)):
                f.append(Finding("H-05", HARD, f"organization {org}: revisions are not gap-free from 1"))

    def rev_ok(label: str, org: str, rev: int | None) -> None:
        if rev is not None and (org, rev) not in ix.cs:
            f.append(Finding("H-05", HARD, f"{label}: revision {rev} has no change set"))

    for table in ("entity_versions", "relationship_versions", "external_identities", "fact_assertions"):
        for r in t[table]:
            if not (sc.has(r["org_id"], r["revision_from"]) or sc.has(r["org_id"], r["revision_to"])):
                continue
            rev_ok(table, r["org_id"], r["revision_from"])
            rev_ok(table, r["org_id"], r["revision_to"])
            if r["revision_to"] is not None and r["revision_to"] <= r["revision_from"]:
                f.append(Finding("H-05", HARD, f"{table}: revision_to <= revision_from"))
    for c in t["change_sets"]:
        if sc.has(c["org_id"], c["revision"]):
            rev_ok("change_set.reverts_revision", c["org_id"], c["reverts_revision"])
            if c["reverts_revision"] is not None and c["reverts_revision"] >= c["revision"]:
                f.append(Finding("H-05", HARD, f"change set {c['revision']} reverts a later revision"))
    for e in t["evidence"]:
        if sc.has(e["org_id"], e["change_revision"]):
            rev_ok("evidence", e["org_id"], e["change_revision"])
    for table, versions, created in (
        ("entity", ix.ev_versions, t["entities"]),
        ("relationship", ix.rv_versions, t["relationships"]),
    ):
        for rec in created:
            vs = versions.get(rec["id"], [])
            if not vs:
                f.append(Finding("H-06", HARD, f"{table} {rec['id']}: no versions"))
                continue
            touched = any(
                sc.has(rec["org_id"], v["revision_from"]) or sc.has(rec["org_id"], v["revision_to"]) for v in vs
            )
            if not (sc.full or touched):
                continue
            if sc.full and vs[0]["revision_from"] != rec["created_revision"]:
                f.append(Finding("H-05", HARD, f"{table} {rec['id']}: created_revision differs from its first version"))
            if sum(v["revision_to"] is None for v in vs) != 1 or vs[-1]["revision_to"] is not None:
                f.append(Finding("H-06", HARD, f"{table} {rec['id']}: exactly one current version required"))
            for a, b in itertools.pairwise(vs):
                if a["revision_to"] != b["revision_from"]:
                    f.append(Finding("H-06", HARD, f"{table} {rec['id']}: versions overlap or leave a gap"))


def _identities(ds: Dataset, ix: Index, sc: Scope, taxes: dict[str, Taxonomy], f: list[Finding]) -> None:
    tax = base_taxonomy(taxes)
    spans: dict[tuple[str, str, str], list[Row]] = defaultdict(list)
    for i in ds.tables["external_identities"]:
        spans[(i["org_id"], i["namespace"], i["normalized_key"])].append(i)
        if not sc.has(i["org_id"], i["revision_from"]):
            continue
        try:
            nk = normalize(tax, i["namespace"], i["original_key"])
        except NormalizationError as exc:
            f.append(Finding("H-08", HARD, f"identity {i['id']}: {exc}"))
            continue
        if nk.normalized_key != i["normalized_key"] or nk.normalization_version != i["normalization_version"]:
            f.append(Finding("H-08", HARD, f"identity {i['id']}: normalized key or version does not follow its rule"))
        if nk.key_parts != i["key_parts"]:
            f.append(Finding("H-08", HARD, f"identity {i['id']}: key_parts differ from the rule's parse"))
    for key, items in spans.items():
        items.sort(key=lambda r: r["revision_from"])
        for a, b in itertools.pairwise(items):
            if a["revision_to"] is None or a["revision_to"] > b["revision_from"]:
                f.append(Finding("H-07", HARD, f"identity {key[1]}:{key[2]} is bound twice at the same time"))
    seen: set[tuple[str, str, str, str, str]] = set()
    for r in ds.tables["relationships"]:
        k = (r["org_id"], r["rel_type"], r["from_id"], r["to_id"], r["discriminator"])
        if k in seen:
            f.append(Finding("H-09", HARD, f"relationship {r['id']}: duplicate (rel_type, from, to, discriminator)"))
        seen.add(k)


# --------------------------------------------------------------------------- DOMAIN: entities and relationships


def conflicting_fields(ix: Index, fact_id: str, rev: int) -> set[str]:
    """Fields with materially different current values: unresolved (TF-2; ADR-0018 conflict procedure)."""
    values: dict[str, set[str]] = defaultdict(set)
    for a in ix.asserts.get(fact_id, []):
        if a["field_path"] and valid_at(a, rev):
            values[a["field_path"]].add(sha256_of(a["asserted_value"]))
    return {p for p, vs in values.items() if len(vs) > 1}


def _entities(ds: Dataset, ix: Index, sc: Scope, taxes: dict[str, Taxonomy], f: list[Finding]) -> None:
    for v in ds.tables["entity_versions"]:
        if not sc.has(v["org_id"], v["revision_from"]):
            continue
        rev = v["revision_from"]
        lbl = f"entity {v['entity_id']}@{rev}"
        tax = taxonomy_for(taxes, v["taxonomy_version"], lbl, f)
        if tax is None:
            continue
        t = tax.types.get(v["type_key"])
        if t is None:
            f.append(Finding("D-01", DOMAIN, f"{lbl}: unknown type {v['type_key']} in taxonomy {tax.version}"))
            continue
        for name in v["attributes"]:
            if not tax.attr_name_re.match(name) or tax.forbidden_attr_re.search(name):
                f.append(Finding("H-13", HARD, f"{lbl}: attribute name {name!r} is not allowed"))
        if v["record_state"] != "present":
            continue
        unresolved = conflicting_fields(ix, v["entity_id"], rev)
        for name in t["required_attributes"]:
            if name not in v["attributes"] and f"attributes.{name}" not in unresolved:
                f.append(Finding("D-01", DOMAIN, f"{lbl}: missing required attribute {name}"))
        for name, spec in t["attribute_enums"].items():
            if name in v["attributes"] and v["attributes"][name] not in tax.enum_values(spec):
                f.append(Finding("D-01", DOMAIN, f"{lbl}: {name}={v['attributes'][name]!r} not in vocabulary"))
        _entity_kind_rules(ix, tax, t, v, lbl, f)
        _environment_rules(ix, t, v, lbl, f)
        _identity_namespace_rules(ix, tax, t, v, lbl, f)
        _scope_rules(ix, tax, v, lbl, f)
    for eid, vs in ix.ev_versions.items():
        for a, b in itertools.pairwise(vs):
            if a["type_key"] != b["type_key"] and sc.has(b["org_id"], b["revision_from"]):
                _retype_rule(ix, taxes, eid, a, b, f)


def _entity_kind_rules(ix: Index, tax: Taxonomy, t: Row, v: Row, lbl: str, f: list[Finding]) -> None:
    if v["category"] != t["category"]:
        f.append(Finding("D-02", DOMAIN, f"{lbl}: category {v['category']} differs from its type's {t['category']}"))
    if t["kind"] == "logical":
        if v["provider"] is not None or v["native_type"] is not None:
            f.append(Finding("D-02", DOMAIN, f"{lbl}: logical types have no provider or native_type"))
        return
    if v["provider"] not in tax.providers:
        f.append(Finding("D-02", DOMAIN, f"{lbl}: unknown or missing provider {v['provider']}"))
    if not v["native_type"] and v["type_key"] != "azure.tenant":  # U-2: the tenant has no ARM type
        f.append(Finding("D-02", DOMAIN, f"{lbl}: physical entities need native_type"))
    if t["provider"] not in ("any", "@cluster") and v["provider"] != t["provider"]:
        f.append(Finding("D-02", DOMAIN, f"{lbl}: {v['type_key']} is a {t['provider']} type"))
    if "native_type" in t and v["native_type"] != t["native_type"]:
        f.append(Finding("D-02", DOMAIN, f"{lbl}: native_type must be {t['native_type']}"))
    if v["provider"] in tax.platform_providers and not v["attributes"].get("platform"):
        f.append(Finding("D-02", DOMAIN, f"{lbl}: provider {v['provider']} requires a platform attribute"))
    if t["provider"] == "@cluster" and v["container_id"]:
        parent = ix.entity_at(v["container_id"], v["revision_from"])
        if parent and parent["provider"] != v["provider"]:
            f.append(Finding("D-02", DOMAIN, f"{lbl}: Kubernetes objects take their cluster's provider"))


def _environment_rules(ix: Index, t: Row, v: Row, lbl: str, f: list[Finding]) -> None:
    scope = t["environment_scope"]
    if scope == "none" and v["environment_scope"] is not None:
        f.append(Finding("D-03", DOMAIN, f"{lbl}: {v['type_key']} is environment-neutral"))
    if scope == "required" and v["environment_scope"] is None:
        f.append(Finding("D-03", DOMAIN, f"{lbl}: environment scope required (single, shared or unknown)"))
    if v["environment_id"]:
        env = ix.entity_at(v["environment_id"], v["revision_from"])
        if env is None or env["type_key"] != "core.environment":
            f.append(Finding("D-03", DOMAIN, f"{lbl}: environment_id is not a core.environment"))


def _identity_namespace_rules(ix: Index, tax: Taxonomy, t: Row, v: Row, lbl: str, f: list[Finding]) -> None:
    allowed = t["identity_namespaces"]
    for i in ix.identities.get(v["entity_id"], []):
        if not valid_at(i, v["revision_from"]):
            continue
        source = i["namespace"].startswith("source.")
        ns = tax.namespaces.get(i["namespace"]) or (tax.namespaces["source.*"] if source else None)
        ok = (
            i["namespace"] in allowed
            or (source and "source.*" in allowed)
            or ("provider_namespace" in allowed and ns is not None and ns["provider"] == v["provider"])
        )
        if not ok:
            f.append(
                Finding("D-15", DOMAIN, f"{lbl}: identity namespace {i['namespace']} not allowed for {v['type_key']}")
            )


def _scope_rules(ix: Index, tax: Taxonomy, v: Row, lbl: str, f: list[Finding]) -> None:
    if v["type_key"] != "infra.scope" or v["provider"] not in tax.scope_rules:
        return
    kinds = {s["scope_kind"]: s["parents"] for s in tax.scope_rules[v["provider"]]["scope_kinds"]}
    kind = v["attributes"].get("scope_kind")
    if kind not in kinds:
        f.append(Finding("D-16", DOMAIN, f"{lbl}: scope_kind {kind!r} not defined for provider {v['provider']}"))
    elif v["container_id"]:
        parent = ix.entity_at(v["container_id"], v["revision_from"])
        if parent and parent["type_key"] == "infra.scope" and parent["attributes"].get("scope_kind") not in kinds[kind]:
            f.append(Finding("D-16", DOMAIN, f"{lbl}: {kind} cannot sit in {parent['attributes'].get('scope_kind')}"))


def is_automatic_retype(tax: Taxonomy, a: Row, b: Row) -> bool:
    """ADR-0017 §2.4: neutral -> provider-specific with the same native type, or generic -> newly mapped category."""
    target = tax.types[b["type_key"]]
    specific = (
        a["type_key"].startswith("infra.")
        and not b["type_key"].startswith("infra.")
        and b["provider"] == a["provider"]
        and target.get("native_type") == a["native_type"]
    )
    mapped = (
        a["type_key"] == "infra.resource"
        and b["type_key"].startswith("infra.")
        and tax.mappings.get(a["provider"] or "", {}).get(a["native_type"] or "") == target["category"]
    )
    return bool(specific or mapped)


def _retype_rule(ix: Index, taxes: dict[str, Taxonomy], eid: str, a: Row, b: Row, f: list[Finding]) -> None:
    cs = ix.cs.get((b["org_id"], b["revision_from"]))
    tax = taxes.get(b["taxonomy_version"])
    if tax is None or b["type_key"] not in tax.types:
        return
    automatic = is_automatic_retype(tax, a, b)
    if not automatic and cs and cs["kind"] not in ("steward_edit", "migration"):
        f.append(
            Finding(
                "D-10",
                DOMAIN,
                f"entity {eid}: re-type {a['type_key']} -> {b['type_key']} needs a steward or migration change set",
            )
        )
    if cs and cs["kind"] == "migration" and not automatic:
        f.append(Finding("D-10", DOMAIN, f"entity {eid}: a migration may only apply automatic re-typing"))


def _arm_key(ix: Index, eid: str, rev: int) -> str | None:
    return next(
        (i["normalized_key"] for i in ix.identities.get(eid, []) if i["namespace"] == "azure.arm" and valid_at(i, rev)),
        None,
    )


def _relationships(ds: Dataset, ix: Index, sc: Scope, taxes: dict[str, Taxonomy], f: list[Finding]) -> None:
    for v in ds.tables["relationship_versions"]:
        if not sc.has(v["org_id"], v["revision_from"]):
            continue
        r = ix.rel[v["relationship_id"]]
        rev = v["revision_from"]
        lbl = f"relationship {r['id']} {r['rel_type']}@{rev}"
        tax = taxonomy_for(taxes, v["taxonomy_version"], lbl, f)
        if tax is None:
            continue
        for name in v["attributes"]:
            if not tax.attr_name_re.match(name) or tax.forbidden_attr_re.search(name):
                f.append(Finding("H-13", HARD, f"{lbl}: attribute name {name!r} is not allowed"))
        if v["record_state"] != "present":
            continue
        fv, tv = ix.entity_at(r["from_id"], rev), ix.entity_at(r["to_id"], rev)
        if fv is None or tv is None:
            continue  # reported by H-10
        f.extend(
            Finding("D-04", DOMAIN, f"{lbl}: {reason}")
            for reason in check_pair(tax, r["rel_type"], fv, tv, v["attributes"])
        )
        rule = tax.rels.get(r["rel_type"])
        if rule is None:
            continue
        if discriminator_of(tax, r["rel_type"], v["attributes"]) != r["discriminator"]:
            f.append(Finding("H-09", HARD, f"{lbl}: discriminator differs from the attribute that defines it"))
        kinds = {
            a["evidence_kind"] for a in ix.asserts.get(r["id"], []) if a["field_path"] is None and valid_at(a, rev)
        }
        bad = kinds - set(rule["allowed_evidence_kinds"])
        if bad:
            f.append(Finding("D-05", DOMAIN, f"{lbl}: evidence kinds {sorted(bad)} not allowed for {r['rel_type']}"))
        if r["rel_type"] == "DEPENDS_ON" and v["trust_state"] == "discovered":
            f.append(Finding("D-11", DOMAIN, f"{lbl}: DEPENDS_ON can never be discovered"))
        for k, val in relationship_flags(fv, tv).items():
            if v[k] != val:
                f.append(Finding("D-08", DOMAIN, f"{lbl}: {k} is {v[k]!r}, endpoints imply {val!r}"))
        if v["cross_environment"] and rule["cross_environment_allowed"] is False:
            f.append(Finding("D-08", DOMAIN, f"{lbl}: {r['rel_type']} is never cross-environment"))
        if r["rel_type"] == "CONTAINS":
            pk, ck = _arm_key(ix, r["from_id"], rev), _arm_key(ix, r["to_id"], rev)
            if pk and ck and not ck.startswith(pk + "/"):
                f.append(Finding("D-06", DOMAIN, f"{lbl}: the child's ARM ID is not inside the parent's"))


# --------------------------------------------------------------------------- per revision (ADR-0008 §5)


def _per_revision(ds: Dataset, ix: Index, sc: Scope, f: list[Finding]) -> None:
    for org, revs in sc.revisions.items():
        for rev in sorted(revs):
            st = as_of(ds, org, rev)
            parents: dict[str, list[str]] = defaultdict(list)
            members: set[str] = set()
            for rid, r in st.relationships.items():
                if r["from_id"] not in st.entities or r["to_id"] not in st.entities:
                    f.append(Finding("H-10", HARD, f"r{rev}: relationship {rid} has an endpoint that is not present"))
                    continue
                if r["rel_type"] == "CONTAINS":
                    parents[r["to_id"]].append(r["from_id"])
                if r["rel_type"] == "MEMBER_OF" and st.entities[r["to_id"]]["type_key"] == "core.environment":
                    members.add(r["from_id"])
            for child, ps in parents.items():
                if len(ps) > 1:
                    f.append(Finding("H-14", HARD, f"r{rev}: entity {child} has {len(ps)} containment parents"))
            for eid, v in st.entities.items():
                if v["container_id"] != parents.get(eid, [None])[0]:
                    f.append(
                        Finding("D-09", DOMAIN, f"r{rev}: entity {eid} container_id differs from its CONTAINS parent")
                    )
                if v["environment_scope"] == "shared" and eid not in members:
                    f.append(Finding("D-03", DOMAIN, f"r{rev}: shared entity {eid} is a member of no environment"))
            for start in parents:
                seen: set[str] = set()
                cur = start
                while cur in parents and cur not in seen:
                    seen.add(cur)
                    cur = parents[cur][0]
                if cur in seen:
                    f.append(Finding("H-14", HARD, f"r{rev}: containment cycle through {start}"))
                    break


# --------------------------------------------------------------------------- change sets (ADR-0008 §7, §10)


def _change_sets(ds: Dataset, ix: Index, sc: Scope, taxes: dict[str, Taxonomy], f: list[Finding]) -> None:
    tax = base_taxonomy(taxes)
    for c in ds.tables["change_sets"]:
        if not sc.has(c["org_id"], c["revision"]):
            continue
        lbl = f"change set {c['org_id']}#{c['revision']}"
        a = c["actor"]
        ns = tax.actor_namespaces.get(a["namespace"])
        if ns is None or ns["actor_kind"] != a["kind"]:
            f.append(Finding("H-11", HARD, f"{lbl}: actor namespace {a['namespace']} is not bound to kind {a['kind']}"))
        if c["channel"] in SERVICE_CHANNELS and a["kind"] != "service":
            f.append(Finding("D-13", DOMAIN, f"{lbl}: channel {c['channel']} must be attributed to a service identity"))
        if c["channel"] == "migration" and a["kind"] not in ("system", "service"):
            f.append(Finding("D-13", DOMAIN, f"{lbl}: migrations are attributed to a system or service identity"))
        if c["kind"] in HUMAN_KINDS and a["kind"] != "human":
            f.append(Finding("D-13", DOMAIN, f"{lbl}: {c['kind']} is a human decision"))
        if (c["kind"] == "migration") != (c["channel"] == "migration"):
            f.append(Finding("D-13", DOMAIN, f"{lbl}: kind migration and channel migration go together"))
        if c["kind"] == "revert":
            _revert_rule(ix, c, f)


def _content(v: Row) -> Row:
    return {k: x for k, x in v.items() if k not in REVISION_FIELDS}


def _revert_rule(ix: Index, c: Row, f: list[Finding]) -> None:
    target, rev = c["reverts_revision"], c["revision"]
    for versions in (ix.ev_versions, ix.rv_versions):
        for fid, vs in versions.items():
            if not any(v["org_id"] == c["org_id"] and v["revision_from"] == target for v in vs):
                continue
            before = next((v for v in vs if valid_at(v, target - 1)), None)
            after = next((v for v in vs if valid_at(v, rev)), None)
            if (
                after is None
                or (before is None and after["record_state"] != "removed")
                or (before is not None and _content(before) != _content(after))
            ):
                f.append(
                    Finding("D-14", DOMAIN, f"revert r{rev}: fact {fid} does not equal its state before r{target}")
                )


# --------------------------------------------------------------------------- trust (D-12, D-19, D-20)


def field_value(v: Row, path: str) -> Any:
    if path.startswith("attributes."):
        return v["attributes"].get(path.split(".", 1)[1])
    return v.get(path)


def claim_of(ix: Index, tax: Taxonomy, fact_kind: str, v: Row) -> Claim | None:
    if fact_kind == "entity":
        return Claim("entity", tax.is_physical(v["type_key"]))
    r = ix.rel[v["relationship_id"]]
    ends = [ix.entity_at(r[k], v["revision_from"]) for k in ("from_id", "to_id")]
    rule = tax.rels.get(r["rel_type"])
    if rule is None or ends[0] is None or ends[1] is None:
        return None
    physical = tax.is_physical(ends[0]["type_key"]) or tax.is_physical(ends[1]["type_key"])
    return Claim("relationship", physical, frozenset(rule["allowed_evidence_kinds"]))


def trust_inputs(ix: Index, fact_id: str, rev: int) -> list[Row]:
    out = []
    for a in ix.asserts.get(fact_id, []):
        if valid_at(a, rev):
            e = ix.ev.get(a["evidence_id"], {})
            out.append(
                {**a, "method": e.get("method"), "observed_at": e.get("observed_at"), "valid_to": e.get("valid_to")}
            )
    return out


def _trust(ds: Dataset, ix: Index, sc: Scope, taxes: dict[str, Taxonomy], f: list[Finding]) -> None:
    for table, key, kind in (
        ("entity_versions", "entity_id", "entity"),
        ("relationship_versions", "relationship_id", "relationship"),
    ):
        versions = ix.ev_versions if kind == "entity" else ix.rv_versions
        for v in ds.tables[table]:
            tax = taxes.get(v["taxonomy_version"])
            if tax is None or v["record_state"] != "present":
                continue
            fid = v[key]
            claim = claim_of(ix, tax, kind, v)
            if claim is None:
                continue
            lbl = f"{kind} {fid}@{v['revision_from']}"
            prev = next((p for p in versions[fid] if p["revision_to"] == v["revision_from"]), None)
            last = v["revision_to"] - 1 if v["revision_to"] else ix.heads[v["org_id"]]
            # Scoped (publishing): only versions written in scope; untouched facts are re-evaluated by recompute
            # change sets (contracts/trust-function.md, time-driven changes). Full history: every revision.
            if not sc.full and not sc.has(v["org_id"], v["revision_from"]):
                continue
            for rev in range(v["revision_from"], last + 1):
                if not sc.has(v["org_id"], rev):
                    continue
                problem = trust_problem(ix, ds.policy, claim, v, prev, fid, rev, lbl)
                if problem:
                    f.append(problem)
                    break


def trust_problem(
    ix: Index, policy: dict[str, Any], claim: Claim, v: Row, prev: Row | None, fid: str, rev: int, lbl: str
) -> Finding | None:
    cur = trust_inputs(ix, fid, rev)
    prior = {a["field_path"]: field_value(prev, a["field_path"]) for a in cur if prev and a["field_path"]}
    res = evaluate(claim, cur, v, policy, ix.cs[(v["org_id"], rev)]["created_at"], prior)
    if res.trust_state is None:
        return Finding("D-12", DOMAIN, f"{lbl}: no current evidence applicable to the claim at revision {rev}")
    if res.trust_state != v["trust_state"]:
        return Finding(
            "D-12",
            DOMAIN,
            f"{lbl}: trust_state {v['trust_state']}, trust function gives "
            f"{res.trust_state} ({res.rule}) at revision {rev}",
        )
    if rev == v["revision_from"] and v["verification_state"] == "verified" and res.verification_state != "verified":
        return Finding("D-19", DOMAIN, f"{lbl}: verification not supported: {'; '.join(res.verification_reasons)}")
    wrong = [p for p, val in res.selected.items() if field_value(v, p) != val]
    if wrong:
        return Finding(
            "D-20", DOMAIN, f"{lbl}: stored value of {wrong} differs from the selected value at revision {rev}"
        )
    return None


# --------------------------------------------------------------------------- ADVISORY (never blocks)


def advisory_findings(ds: Dataset, taxes: dict[str, Taxonomy]) -> list[Finding]:
    f: list[Finding] = []
    ix = Index(ds)
    tax = base_taxonomy(taxes)
    for org, head in ix.heads.items():
        st = as_of(ds, org, head)
        rels = st.relationships.values()
        owned = {r["to_id"] for r in rels if r["rel_type"] == "OWNS"}
        deployed_from = {r["from_id"] for r in rels if r["rel_type"] == "DEPLOYED_AS"}
        parent_of = {r["to_id"]: r["from_id"] for r in rels if r["rel_type"] == "CONTAINS"}
        directional = {(r["from_id"], r["to_id"]) for r in rels if r["rel_type"] in ("READS_FROM", "WRITES_TO")}
        for eid, v in sorted(st.entities.items()):
            _entity_advisories(taxes, tax, eid, v, owned, deployed_from, parent_of, f)
        for _rid, r in sorted(st.relationships.items()):
            v = r["version"]
            names = (
                f"{st.entities[r['from_id']]['display_name']} {r['rel_type']} {st.entities[r['to_id']]['display_name']}"
            )
            if v["cross_environment"]:
                f.append(Finding("A-02", ADVISORY, f"cross-environment: {names}"))
            if v["cross_provider"] and v["verification_state"] != "verified":
                f.append(Finding("A-05", ADVISORY, f"unverified cross-provider: {names}"))
            if r["rel_type"] == "STORES_IN" and (r["from_id"], r["to_id"]) not in directional:
                f.append(Finding("A-06", ADVISORY, f"direction not established: {names}"))
            if r["rel_type"] == "DEPLOYED_AS" and not v["attributes"].get("deployment_role"):
                f.append(Finding("A-07", ADVISORY, f"deployment role not established: {names}"))
            if v["trust_state"] in ("stale", "conflicted"):
                f.append(Finding("A-12", ADVISORY, f"{names} is {v['trust_state']}"))
    return f


def _entity_advisories(
    taxes: dict[str, Taxonomy],
    tax: Taxonomy,
    eid: str,
    v: Row,
    owned: set[str],
    deployed_from: set[str],
    parent_of: dict[str, str],
    f: list[Finding],
) -> None:
    tk = v["type_key"]
    if tk in ("core.application", "core.system", "core.service", "core.dataset") and eid not in owned:
        f.append(Finding("A-01", ADVISORY, f"{v['display_name']} ({tk}) has no owner"))
    if v["environment_scope"] == "unknown" and tk != "infra.scope" and v["category"] != "scope":
        f.append(Finding("A-03", ADVISORY, f"{v['display_name']} ({tk}) has unknown environment scope"))
    if tk == "infra.resource":
        f.append(
            Finding("A-04", ADVISORY, f"{v['display_name']} ({v['provider']} {v['native_type']}) is uncategorized")
        )
    if v["category"] != "scope" and tax.is_physical(tk) and v["container_id"] is None:
        f.append(Finding("A-08", ADVISORY, f"{v['display_name']} ({tk}) has no known containment parent"))
    if tk == "core.application":
        services = [c for c, p in parent_of.items() if p == eid]
        if eid not in deployed_from and not any(s in deployed_from for s in services):
            f.append(Finding("A-09", ADVISORY, f"{v['display_name']} has no known deployment"))
    if tk.startswith("infra.") and tk not in ("infra.resource", "infra.scope"):
        mapped = taxes.get(v["taxonomy_version"], tax).mappings.get(v["provider"], {}).get(v["native_type"])
        if mapped != v["category"]:
            f.append(Finding("A-11", ADVISORY, f"{v['display_name']}: native type {v['native_type']} is not mapped"))
    if v["trust_state"] in ("stale", "conflicted"):
        f.append(Finding("A-12", ADVISORY, f"{v['display_name']} is {v['trust_state']}"))


def summary(findings: list[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in findings:
        out[x.rule] = out.get(x.rule, 0) + 1
    return dict(sorted(out.items()))


def blocking(findings: list[Finding]) -> list[Finding]:
    return [x for x in findings if x.cls != ADVISORY]


def describe(findings: list[Finding], limit: int = 20) -> str:
    lines = [str(x) for x in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"... and {len(findings) - limit} more")
    return "\n".join(lines)
