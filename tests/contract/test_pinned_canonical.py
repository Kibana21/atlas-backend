"""The backend's canonical domain against the pinned atlas-specs oracles (ATLAS-002 CP3; run by ``make contract``).

* the packaged taxonomy is byte-identical to the pinned artifact, and the CI role-script copies to the G3-R scripts;
* the trust function passes every case of ``trust-function-v1/cases.json`` (AC-CP3-08);
* the golden fixture: no HARD or DOMAIN finding, the expected ADVISORY counts, every as-of graph hash, every identity
  normalized as stored, every stored trust state reproduced (D-12), and each atlas-specs negative mutation detected
  by its rule. The one difference: secret-like attribute names are H-13 here; atlas-specs reports them through its
  JSON Schema check (H-01).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from scripts.db.check_bootstrap import strip_ci_additions

from atlas.domain.canonical import rules, taxonomy
from atlas.domain.canonical.graph import TABLES, Dataset, as_of, graph_hash
from atlas.domain.canonical.identity import normalize
from atlas.domain.canonical.trust import Claim, evaluate

pytestmark = [pytest.mark.pinned_specs, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build" / "specs"
FIXTURE = SPECS / "acceptance" / "test-data" / "canonical-golden-v1"
CASES = SPECS / "acceptance" / "test-data" / "trust-function-v1" / "cases.json"
PROPOSALS = SPECS / "features" / "ATLAS-002-canonical-graph-foundation" / "proposals"
Mutation = Callable[[Dataset], None]


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.fail(f"pinned specs data missing ({path.relative_to(ROOT)}): run `make specs`")
    return path


@lru_cache(maxsize=1)
def _golden() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[str, taxonomy.Taxonomy]]:
    d = _require(FIXTURE)
    tables = {t: json.loads((d / f"{t}.json").read_text(encoding="utf-8")) for t in TABLES}
    policy = json.loads((d / "trust-policy.json").read_text(encoding="utf-8"))
    base = taxonomy.current()
    taxes = {base.version: base}
    for path in sorted(d.glob("taxonomy-overlay.*.json")):
        overlay = base.with_overlay(json.loads(path.read_text(encoding="utf-8")))
        taxes[overlay.version] = overlay
    return tables, policy, taxes


def golden() -> tuple[Dataset, dict[str, taxonomy.Taxonomy]]:
    tables, policy, taxes = _golden()
    return Dataset(copy.deepcopy(tables), copy.deepcopy(policy)), taxes


# --------------------------------------------------------------------------- pinned artifacts


def test_packaged_taxonomy_is_the_pinned_artifact() -> None:
    pinned = _require(SPECS / "contracts" / "taxonomy" / "atlas-taxonomy.v1.json").read_text(encoding="utf-8")
    assert taxonomy.artifact_text() == pinned


@pytest.mark.parametrize(
    ("copy_name", "proposal"),
    [
        ("bootstrap-ingest.sql", "postgres-ingest-roles.sql"),
        ("bootstrap-ingest-verify.sql", "postgres-ingest-roles-verify.sql"),
        ("bootstrap-ingest-rollback.sql", "postgres-ingest-roles-rollback.sql"),
    ],
)
def test_ci_role_scripts_are_verbatim_copies(copy_name: str, proposal: str) -> None:
    text = (ROOT / "scripts" / "db" / copy_name).read_text(encoding="utf-8")
    assert strip_ci_additions(text, header_only=True) == _require(PROPOSALS / proposal).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- trust function truth table


@lru_cache(maxsize=1)
def _cases() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_require(CASES).read_text(encoding="utf-8"))
    return loaded


def _case_ids() -> list[str]:
    return [c["id"] for c in _cases()["cases"]] if CASES.exists() else ["pinned-specs-missing"]


@pytest.mark.parametrize("case_id", _case_ids())
def test_trust_function_truth_table(case_id: str) -> None:
    doc = _cases()
    assert doc["function"] == "trust-1"
    assert doc["taxonomy_version"] == taxonomy.CURRENT_VERSION
    case = next(c for c in doc["cases"] if c["id"] == case_id)
    i, expected = case["input"], case["expected"]
    c = i["claim"]
    tax = taxonomy.current()
    claim = (
        Claim("relationship", c["physical"], frozenset(tax.rels[c["rel_type"]]["allowed_evidence_kinds"]))
        if c["fact_kind"] == "relationship"
        else Claim("entity", c["physical"])
    )

    def run() -> Any:
        return evaluate(claim, i["assertions"], i["verification"], i["policy"], i["at"], i["prior_values"], i["origin"])

    if "error" in expected:
        with pytest.raises(ValueError, match=expected["error"]):
            run()
        return
    result = run()
    for key, value in expected.items():
        if key != "verification_reasons":
            assert getattr(result, key) == value, key
    assert result.verification_reasons == expected.get("verification_reasons", [])


def test_the_truth_table_has_36_cases() -> None:
    assert len(_cases()["cases"]) == 36


# --------------------------------------------------------------------------- golden fixture


def test_golden_fixture_has_no_blocking_finding() -> None:
    ds, taxes = golden()
    findings = rules.validate(ds, taxes)
    assert findings == [], rules.describe(findings)


def test_golden_fixture_advisories_match_the_expected_report() -> None:
    ds, taxes = golden()
    expected = json.loads((FIXTURE / "expected-findings.json").read_text(encoding="utf-8"))
    assert expected["hard"] == expected["domain"] == 0
    assert rules.summary(rules.advisory_findings(ds, taxes)) == expected["advisory"]


def test_golden_fixture_as_of_hashes() -> None:
    ds, _ = golden()
    keys = {o["key"]: o["id"] for o in ds.tables["organizations"]}
    for e in json.loads((FIXTURE / "expected-as-of.json").read_text(encoding="utf-8")):
        st = as_of(ds, keys[e["org_key"]], e["revision"])
        assert (len(st.entities), len(st.relationships), graph_hash(st)) == (
            e["entities"],
            e["relationships"],
            e["graph_hash"],
        ), e
    for s in ds.tables["graph_snapshots"]:
        assert graph_hash(as_of(ds, s["org_id"], s["revision"])) == s["graph_hash"]


def test_golden_identities_normalize_as_stored() -> None:
    ds, _ = golden()
    tax = taxonomy.current()
    for i in ds.tables["external_identities"]:
        key = normalize(tax, i["namespace"], i["original_key"])
        assert (key.normalized_key, key.key_parts, key.normalization_version) == (
            i["normalized_key"],
            i["key_parts"],
            i["normalization_version"],
        )


def test_golden_trust_states_are_reproduced_by_the_backend_function() -> None:
    ds, taxes = golden()
    ix = rules.Index(ds)
    checked = 0
    for table, kind in (("entity_versions", "entity"), ("relationship_versions", "relationship")):
        for v in ds.tables[table]:
            if v["record_state"] != "present":
                continue
            claim = rules.claim_of(ix, taxes[v["taxonomy_version"]], kind, v)
            assert claim is not None
            fid = v["entity_id" if kind == "entity" else "relationship_id"]
            at = ix.cs[(v["org_id"], v["revision_from"])]["created_at"]
            prev = next(
                (
                    p
                    for p in (ix.ev_versions if kind == "entity" else ix.rv_versions)[fid]
                    if p["revision_to"] == v["revision_from"]
                ),
                None,
            )
            prior = {
                a["field_path"]: rules.field_value(prev, a["field_path"])
                for a in ix.asserts[fid]
                if prev and a["field_path"]
            }
            result = evaluate(claim, rules.trust_inputs(ix, fid, v["revision_from"]), v, ds.policy, at, prior)
            assert result.trust_state == v["trust_state"], (fid, result.rule)
            checked += 1
    assert checked > 800


# --------------------------------------------------------------------------- the atlas-specs negative mutations

N = iter(range(1, 10_000))


def fresh_id() -> str:
    return f"0190f000-0000-7000-8000-9{next(N):011d}"


def by(ds: Dataset, table: str, **match: Any) -> dict[str, Any]:
    return next(r for r in ds.tables[table] if all(r.get(k) == v for k, v in match.items()))


def named(ds: Dataset, name: str, type_key: str | None = None) -> dict[str, Any]:
    return next(
        v
        for v in ds.tables["entity_versions"]
        if v["display_name"] == name and v["revision_to"] is None and (type_key is None or v["type_key"] == type_key)
    )


def _copy_relationship(ds: Dataset, r: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    r = copy.deepcopy(r)
    v = copy.deepcopy(by(ds, "relationship_versions", relationship_id=r["id"], revision_to=None))
    r["id"] = v["relationship_id"] = fresh_id()
    ds.tables["relationships"].append(r)
    ds.tables["relationship_versions"].append(v)
    return r, v


def _add_rel(ds: Dataset, rel_type: str, frm: str, to: str, attrs: dict[str, Any], kind: str = "documentation") -> None:
    fv, tv = named(ds, frm), named(ds, to)
    rid = fresh_id()
    ev = next(e for e in ds.tables["evidence"] if e["org_id"] == fv["org_id"] and e["kind"] == kind)
    ds.tables["relationships"].append(
        {
            "org_id": fv["org_id"],
            "id": rid,
            "rel_type": rel_type,
            "from_id": fv["entity_id"],
            "to_id": tv["entity_id"],
            "discriminator": "",
            "created_revision": 8,
        }
    )
    ds.tables["relationship_versions"].append(
        {
            "org_id": fv["org_id"],
            "relationship_id": rid,
            "revision_from": 8,
            "revision_to": None,
            "record_state": "present",
            "lifecycle": "active",
            "environment_id": None,
            "cross_environment": False,
            "shared_endpoint": False,
            "cross_provider": False,
            "attributes": attrs,
            "trust_state": "documented",
            "verification_state": "unverified",
            "verified_by": None,
            "verified_at": None,
            "expires_at": None,
            "taxonomy_version": taxonomy.CURRENT_VERSION,
        }
    )
    ds.tables["fact_assertions"].append(
        {
            "org_id": fv["org_id"],
            "id": fresh_id(),
            "fact_kind": "relationship",
            "fact_id": rid,
            "field_path": None,
            "asserted_value": None,
            "evidence_id": ev["id"],
            "evidence_kind": ev["kind"],
            "revision_from": 8,
            "revision_to": None,
        }
    )


def m_cross_org(ds: Dataset) -> None:
    a, b = ds.org_ids()
    env_a = next(
        v["entity_id"] for v in ds.tables["entity_versions"] if v["org_id"] == a and v["type_key"] == "core.environment"
    )
    next(v for v in ds.tables["entity_versions"] if v["org_id"] == b and v["environment_scope"] == "single")[
        "environment_id"
    ] = env_a


def m_bad_interval(ds: Dataset) -> None:
    v = next(v for v in ds.tables["entity_versions"] if v["revision_to"] is not None)
    v["revision_to"] = v["revision_from"]


def m_two_current(ds: Dataset) -> None:
    next(v for v in ds.tables["entity_versions"] if v["revision_to"] is not None)["revision_to"] = None


def m_duplicate_identity(ds: Dataset) -> None:
    i = copy.deepcopy(by(ds, "external_identities", namespace="azure.arm"))
    i["id"] = fresh_id()
    i["entity_id"] = next(
        e["id"] for e in ds.tables["entities"] if e["org_id"] == i["org_id"] and e["id"] != i["entity_id"]
    )
    ds.tables["external_identities"].append(i)


def m_unnormalized(ds: Dataset) -> None:
    i = next(
        i
        for i in ds.tables["external_identities"]
        if i["namespace"] == "azure.arm" and i["original_key"] != i["normalized_key"]
    )
    i["normalized_key"] = i["original_key"]


def m_planned_namespace(ds: Dataset) -> None:
    by(ds, "external_identities", namespace="aws.arn")["namespace"] = "huawei.resource"


def m_duplicate_relationship(ds: Dataset) -> None:
    _copy_relationship(ds, by(ds, "relationships", rel_type="READS_FROM"))


def m_dangling(ds: Dataset) -> None:
    r = by(ds, "relationships", rel_type="WRITES_TO")
    next(v for v in ds.tables["entity_versions"] if v["entity_id"] == r["to_id"] and v["revision_to"] is None)[
        "record_state"
    ] = "removed"


def m_two_parents(ds: Dataset) -> None:
    child, other = named(ds, "Claims Intake", "core.capability"), named(ds, "Quoting", "core.capability")
    r = next(r for r in ds.tables["relationships"] if r["rel_type"] == "CONTAINS" and r["to_id"] == child["entity_id"])
    _copy_relationship(ds, r)[0]["from_id"] = other["entity_id"]


def m_cycle(ds: Dataset) -> None:
    root, leaf = named(ds, "Insurance Operations"), named(ds, "Claims Intake", "core.capability")
    r = next(r for r in ds.tables["relationships"] if r["rel_type"] == "CONTAINS" and r["to_id"] == leaf["entity_id"])
    new, _ = _copy_relationship(ds, r)
    new["from_id"], new["to_id"] = leaf["entity_id"], root["entity_id"]
    root["container_id"] = leaf["entity_id"]


def m_secret_attribute(ds: Dataset) -> None:
    named(ds, "kv-claims-prod")["attributes"]["client_secret"] = "x"  # noqa: S105 - a forbidden attribute name is the test


def m_generic_read(ds: Dataset) -> None:
    _add_rel(ds, "READS_FROM", "Claims API Service", "afd-customer-prod", {})


def m_reserved(ds: Dataset) -> None:
    _add_rel(ds, "PROTECTS", "kv-claims-prod", "sqldb-claims", {})


def m_depends_on_provider(ds: Dataset) -> None:
    _add_rel(ds, "DEPENDS_ON", "Claims Engine", "Billing Hub", {"rationale": "x"}, kind="provider_configuration")


def m_cross_provider_containment(ds: Dataset) -> None:
    _add_rel(ds, "CONTAINS", "example-oci-tenancy", "rg-claims-dr", {})


def m_wrong_flag(ds: Dataset) -> None:
    next(v for v in ds.tables["relationship_versions"] if v["cross_environment"])["cross_environment"] = False


def m_retype_by_import(ds: Dataset) -> None:
    org = ds.org_ids()[0]
    named(ds, "Quotation Service").update(
        type_key="core.application", category="core.application", attributes={"org_key": "APP-X"}
    )
    by(ds, "change_sets", org_id=org, revision=3).update(
        kind="import",
        channel="import",
        actor={"kind": "service", "namespace": "atlas.service", "key": "import-worker", "label": None},
    )


def m_human_import(ds: Dataset) -> None:
    by(ds, "change_sets", revision=2)["actor"] = {
        "kind": "human",
        "namespace": "oidc",
        "key": "https://x.invalid/x|y",
        "label": None,
    }


def m_trust_without_evidence(ds: Dataset) -> None:
    named(ds, "Claims Portal", "core.application")["trust_state"] = "discovered"


def m_environment_not_env(ds: Dataset) -> None:
    v = next(v for v in ds.tables["entity_versions"] if v["environment_scope"] == "single" and v["revision_to"] is None)
    v["environment_id"] = named(ds, "Claims Portal", "core.application")["entity_id"]


def m_scope_nesting(ds: Dataset) -> None:
    named(ds, "claims", "infra.scope")["attributes"]["scope_kind"] = "project"
    named(ds, "claims-events-prod")["attributes"]["scope_kind"] = "organization"


def m_revert_mismatch(ds: Dataset) -> None:
    org = ds.org_ids()[0]
    next(v for v in ds.tables["relationship_versions"] if v["org_id"] == org and v["revision_from"] == 5)[
        "attributes"
    ] = {"x": True}


def _rel_version(ds: Dataset, rel_type: str, frm: str, to: str) -> dict[str, Any]:
    r = by(
        ds, "relationships", rel_type=rel_type, from_id=named(ds, frm)["entity_id"], to_id=named(ds, to)["entity_id"]
    )
    return next(
        v for v in ds.tables["relationship_versions"] if v["relationship_id"] == r["id"] and v["revision_to"] is None
    )


def m_stale_recorded_as_documented(ds: Dataset) -> None:
    named(ds, "mq-legacy-01")["trust_state"] = "documented"


def m_declared_as_observed(ds: Dataset) -> None:
    _rel_version(ds, "CONTAINS", "claims-events-prod", "document-indexed")["trust_state"] = "discovered"


def m_logical_from_provider_only(ds: Dataset) -> None:
    app = named(ds, "Claims Portal", "core.application")
    prov = next(
        e for e in ds.tables["evidence"] if e["org_id"] == app["org_id"] and e["kind"] == "provider_configuration"
    )
    for a in ds.tables["fact_assertions"]:
        if a["fact_id"] == app["entity_id"] and a["field_path"] is None:
            a.update(evidence_id=prov["id"], evidence_kind="provider_configuration")
    app["trust_state"] = "discovered"


def m_verified_declared_only(ds: Dataset) -> None:
    _rel_version(ds, "CONTAINS", "claims-events-prod", "document-indexed").update(
        verification_state="verified",
        verified_by={"namespace": "oidc", "key": "https://login.example.invalid/synthetic|steward-0001"},
        verified_at="2026-09-20T10:00:00Z",
        expires_at=None,
    )


def m_verified_by_service(ds: Dataset) -> None:
    v = next(
        v
        for v in ds.tables["relationship_versions"]
        if v["verification_state"] == "verified" and v["revision_to"] is None
    )
    v["verified_by"] = {"namespace": "atlas.service", "key": "import-worker"}


def m_conflicted_value_stored(ds: Dataset) -> None:
    named(ds, "vm-batch-prod")["attributes"]["location"] = "westeurope"


def m_unresolved_without_conflict(ds: Dataset) -> None:
    vm = named(ds, "vm-batch-prod")
    ds.tables["fact_assertions"] = [
        a
        for a in ds.tables["fact_assertions"]
        if not (a["fact_id"] == vm["entity_id"] and a["asserted_value"] == "northeurope")
    ]


def revert_of_r7(ds: Dataset) -> dict[str, Any]:
    """Revision 8 reverts the taxonomy migration r7 (infra.resource -> infra.data_store under 1.2.0-test.1)."""
    org = ds.org_ids()[0]
    ds.tables.update(_at(ds, 7).tables)
    r6, r7 = (by(ds, "change_sets", org_id=org, revision=r) for r in (6, 7))
    current = next(v for v in ds.tables["entity_versions"] if v["org_id"] == org and v["revision_from"] == 7)
    before = next(
        v for v in ds.tables["entity_versions"] if v["entity_id"] == current["entity_id"] and v["revision_to"] == 7
    )
    current["revision_to"] = 8
    restored = {**copy.deepcopy(before), "revision_from": 8, "revision_to": None}
    ds.tables["entity_versions"].append(restored)
    ds.tables["change_sets"].append(
        {
            **copy.deepcopy(r6),
            "revision": 8,
            "kind": "revert",
            "reverts_revision": 7,
            "reason": "undo the taxonomy migration",
            "source_ref": "revert of r7",
            "input_hash": None,
            "change_reference": None,
            "created_at": r7["created_at"],
        }
    )
    return restored


def m_revert_restamps_taxonomy(ds: Dataset) -> None:
    revert_of_r7(ds)["taxonomy_version"] = "1.2.0-test.1"


def m_revert_to_another_type(ds: Dataset) -> None:
    revert_of_r7(ds).update(type_key="infra.compute", category="compute_host")


def m_retype_strands_relationship(ds: Dataset) -> None:
    """A steward re-types the r7 data store back to infra.resource at r9, leaving r8's READS_FROM in place."""
    org = ds.org_ids()[0]
    current = next(v for v in ds.tables["entity_versions"] if v["org_id"] == org and v["revision_from"] == 7)
    before = next(
        v for v in ds.tables["entity_versions"] if v["entity_id"] == current["entity_id"] and v["revision_to"] == 7
    )
    current["revision_to"] = 9
    ds.tables["entity_versions"].append(
        {
            **copy.deepcopy(current),
            "type_key": before["type_key"],
            "category": before["category"],
            "revision_from": 9,
            "revision_to": None,
        }
    )
    r8 = by(ds, "change_sets", org_id=org, revision=8)
    ds.tables["change_sets"].append(
        {
            **copy.deepcopy(by(ds, "change_sets", org_id=org, revision=6)),
            "revision": 9,
            "reason": "re-type",
            "source_ref": "steward",
            "created_at": r8["created_at"],
        }
    )


def test_atlas_specs_retype_with_unloaded_relationship_taxonomy_is_d01_not_skipped() -> None:
    """F-10: the D-04 re-check needs the relationship's recorded taxonomy; without it the re-type is D-01."""
    ds, taxes = golden()
    m_retype_strands_relationship(ds)
    org = ds.org_ids()[0]
    by(ds, "entity_versions", org_id=org, revision_from=9)["taxonomy_version"] = taxonomy.CURRENT_VERSION
    approved = {taxonomy.CURRENT_VERSION: taxes[taxonomy.CURRENT_VERSION]}
    found = rules.validate(ds, approved, {org: {9}})  # only the re-type is in scope
    assert {f.rule for f in found} == {"D-01"}
    assert all("re-typed" in f.message and "unknown taxonomy version 1.2.0-test.1" in f.message for f in found)
    assert "D-04" in {f.rule for f in rules.validate(ds, taxes, {org: {9}})}  # every taxonomy loaded


def test_a_reclassification_revert_restores_the_earlier_type_and_taxonomy_version() -> None:
    ds, taxes = golden()
    restored = revert_of_r7(ds)
    assert (restored["type_key"], restored["taxonomy_version"]) == ("infra.resource", "1.1.0")
    assert rules.validate(ds, taxes) == []
    org = ds.org_ids()[0]
    assert graph_hash(as_of(ds, org, 8)) == graph_hash(as_of(ds, org, 6))
    assert graph_hash(as_of(ds, org, 7)) != graph_hash(as_of(ds, org, 6))


MUTATIONS: list[tuple[Mutation, str]] = [
    (m_cross_org, "H-03"),
    (m_bad_interval, "H-05"),
    (m_two_current, "H-06"),
    (m_duplicate_identity, "H-07"),
    (m_unnormalized, "H-08"),
    (m_planned_namespace, "H-08"),
    (m_duplicate_relationship, "H-09"),
    (m_dangling, "H-10"),
    (m_two_parents, "H-14"),
    (m_cycle, "H-14"),
    (m_secret_attribute, "H-13"),
    (m_generic_read, "D-04"),
    (m_reserved, "D-04"),
    (m_depends_on_provider, "D-05"),
    (m_cross_provider_containment, "D-04"),
    (m_wrong_flag, "D-08"),
    (m_retype_by_import, "D-10"),
    (m_human_import, "D-13"),
    (m_trust_without_evidence, "D-12"),
    (m_environment_not_env, "D-03"),
    (m_scope_nesting, "D-16"),
    (m_revert_mismatch, "D-14"),
    (m_stale_recorded_as_documented, "D-12"),
    (m_declared_as_observed, "D-12"),
    (m_logical_from_provider_only, "D-12"),
    (m_verified_declared_only, "D-19"),
    (m_verified_by_service, "D-19"),
    (m_conflicted_value_stored, "D-20"),
    (m_unresolved_without_conflict, "D-01"),
    (m_revert_restamps_taxonomy, "D-14"),
    (m_revert_to_another_type, "D-10"),
    (m_retype_strands_relationship, "D-04"),
]


@pytest.mark.parametrize(("mutate", "rule"), MUTATIONS, ids=[m.__name__ for m, _ in MUTATIONS])
def test_atlas_specs_negative_mutation_is_detected(mutate: Mutation, rule: str) -> None:
    ds, taxes = golden()
    mutate(ds)
    assert rule in {f.rule for f in rules.validate(ds, taxes)}


# --------------------------------------------------------------------------- CP4: import planner, replay drafts, revert


def _at(ds: Dataset, revision: int) -> Dataset:
    """The fixture's history as it stood at ``revision`` (later rows dropped, later closings undone)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in ds.tables.items():
        keep = []
        for r in rows:
            start = r.get("revision_from", r.get("revision", r.get("created_revision", r.get("change_revision", 0))))
            if start is not None and start > revision:
                continue
            r = dict(r)
            if r.get("revision_to") is not None and r["revision_to"] > revision:
                r["revision_to"] = None
            keep.append(r)
        out[name] = keep
    return Dataset(out, ds.policy)


def test_import_plan_reproduces_revision_2_and_its_report() -> None:
    from atlas.domain.canonical.importer import plan_batch

    ds, taxes = golden()
    batch = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.json").read_text(encoding="utf-8"))
    report = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.expected-report.json").read_text(encoding="utf-8"))
    org = next(o["id"] for o in ds.tables["organizations"] if o["key"] == batch["org_key"])
    plan = plan_batch(taxes[batch["taxonomy_version"]], batch, as_of(ds, org, 1))
    created = [r for r in ds.tables["relationships"] if r["org_id"] == org and r["created_revision"] == 2]
    assert {d["entity_id"] for d in plan.new_entities} == {
        e["id"] for e in ds.tables["entities"] if e["org_id"] == org and e["created_revision"] == 2
    }
    assert {d["id"] for d in plan.new_relationships} == {r["id"] for r in created if r["rel_type"] != "CONTAINS"}
    assert set(plan.new_containments) == {(r["from_id"], r["to_id"]) for r in created if r["rel_type"] == "CONTAINS"}
    assert plan.report() == report["rejected"]
    for rev in (2, ds.head(org)):
        assert plan_batch(taxes[batch["taxonomy_version"]], batch, as_of(ds, org, rev)).empty  # replay is a no-op


def test_recorded_drafts_cover_every_revision() -> None:
    from atlas.persistence.canonical import history

    ds, _ = golden()
    org = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    recs = history.recorded_drafts(ds, org)
    assert [r.revision for r in recs] == list(range(1, 9))
    assert [r.taxonomy_version for r in recs] == ["1.1.0"] * 6 + ["1.2.0-test.1"] * 2
    assert sum(len(r.draft.assertions) for r in recs) == sum(
        1 for a in ds.tables["fact_assertions"] if a["org_id"] == org
    )


def test_revert_draft_of_revision_4_equals_the_recorded_revert() -> None:
    from atlas.persistence.canonical import history
    from atlas.persistence.canonical.publish import PublishRejected

    ds, _ = golden()
    org = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    draft = history.revert_draft(_at(ds, 4), org, 4, 4, history.new_id, "revert")
    recorded = [v for v in ds.tables["relationship_versions"] if v["org_id"] == org and v["revision_from"] == 5]
    assert [r.relationship_id for r in draft.relationships] == [v["relationship_id"] for v in recorded]
    assert [(r.record_state, r.attributes) for r in draft.relationships] == [
        (v["record_state"], v["attributes"]) for v in recorded
    ]
    closed_at_5 = {a["id"] for a in ds.tables["fact_assertions"] if a["org_id"] == org and a["revision_to"] == 5}
    assert set(draft.close_assertions) == closed_at_5
    with pytest.raises(PublishRejected):  # revision 4's fact was changed again by the revert at r5
        history.revert_draft(ds, org, 4, ds.head(org), history.new_id, "again")
