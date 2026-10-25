"""Canonical validation rules on a small synthetic graph (atlas-specs canonical-validation-rules.md).

The golden fixture is checked in the pinned-specs suite; these tests prove each rule fires on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atlas.domain.canonical import rules, taxonomy
from atlas.domain.canonical.graph import Dataset, as_of, graph_hash
from tests.unit import canonical_graph as g

pytestmark = pytest.mark.unit
Mutation = Callable[[Dataset], None]


def found(ds: Dataset, scope: dict[str, set[int]] | None = None) -> set[str]:
    return {f.rule for f in rules.validate(ds, g.TAXES, scope)}


def ev(ds: Dataset, entity_id: str) -> dict[str, Any]:
    return g.current(ds, "entity_versions", entity_id=entity_id)


def rv(ds: Dataset, rid: str) -> dict[str, Any]:
    return g.current(ds, "relationship_versions", relationship_id=rid)


def test_the_synthetic_graph_is_valid() -> None:
    ds = g.build()
    assert rules.validate(ds, g.TAXES) == []
    assert rules.validate(ds, g.TAXES, {g.ORG: {1}}) == []
    assert graph_hash(as_of(ds, g.ORG, 1)).startswith("sha256:")


def test_advisories_never_block_and_are_counted() -> None:
    ds = g.build()
    assert rules.advisory_findings(ds, g.TAXES) == []
    rv(ds, g.uid(33))["attributes"] = {}  # DEPLOYED_AS without a deployment role
    ev(ds, g.APP)["display_name"] = "Claims Portal"
    ds.tables["relationship_versions"] = [
        v for v in ds.tables["relationship_versions"] if v["relationship_id"] != g.uid(34)
    ]
    ds.tables["relationships"] = [r for r in ds.tables["relationships"] if r["id"] != g.uid(34)]
    findings = rules.advisory_findings(ds, g.TAXES)
    assert rules.summary(findings) == {"A-01": 1, "A-07": 1}  # no owner; no deployment role
    assert rules.blocking(findings) == []
    assert "and 1 more" in rules.describe(findings, limit=1)


def _second_revision(ds: Dataset, kind: str = "steward_edit", actor_kind: str = "human") -> None:
    actor = {"kind": "human", "namespace": "oidc", "key": g.STEWARD["key"], "label": None}
    if actor_kind == "service":
        actor = {"kind": "service", "namespace": "atlas.service", "key": "import-worker", "label": None}
    ds.tables["change_sets"].append(
        {
            **ds.tables["change_sets"][0],
            "revision": 2,
            "kind": kind,
            "channel": "migration" if kind == "migration" else "api",
            "actor": actor,
            "created_at": "2026-10-02T09:00:00Z",
        }
    )


def _retype(ds: Dataset, kind: str) -> None:
    _second_revision(ds, kind, "service" if kind == "import" else "human")
    old = ev(ds, g.APP)
    old["revision_to"] = 2
    ds.tables["entity_versions"].append(
        {
            **old,
            "revision_from": 2,
            "revision_to": None,
            "type_key": "core.service",
            "category": "core.service",
            "attributes": {},
        }
    )


def m_cross_org_reference(ds: Dataset) -> None:
    ev(ds, g.WEB)["environment_id"] = "0190f000-0000-7000-8000-999999999999"


def m_unknown_org(ds: Dataset) -> None:
    ds.tables["external_identities"][0]["org_id"] = "0190f000-0000-7000-8000-999999999998"


def m_revision_without_change_set(ds: Dataset) -> None:
    ev(ds, g.TEAM)["revision_from"] = 7


def m_two_current_versions(ds: Dataset) -> None:
    ds.tables["entity_versions"].append(dict(ev(ds, g.TEAM)))


def m_duplicate_ids(ds: Dataset) -> None:
    ds.tables["evidence"].append(dict(ds.tables["evidence"][0]))


def m_identity_bound_twice(ds: Dataset) -> None:
    ds.tables["external_identities"].append({**ds.tables["external_identities"][3], "id": g.uid(79), "entity_id": g.RG})


def m_unnormalized_key(ds: Dataset) -> None:
    ds.tables["external_identities"][3]["normalized_key"] = g.ARM_SUB.upper()


def m_invalid_key(ds: Dataset) -> None:
    ds.tables["external_identities"][4]["original_key"] = "subscriptions/not-an-arm-id"


def m_duplicate_relationship(ds: Dataset) -> None:
    dup = {**ds.tables["relationships"][4], "id": g.uid(39)}
    ds.tables["relationships"].append(dup)
    ds.tables["relationship_versions"].append({**rv(ds, g.uid(34)), "relationship_id": dup["id"]})


def m_self_relationship(ds: Dataset) -> None:
    ds.tables["relationships"][4]["to_id"] = g.TEAM


def m_dangling(ds: Dataset) -> None:
    v = ev(ds, g.TEAM)
    v["record_state"] = "removed"


def m_two_parents(ds: Dataset) -> None:
    ds.tables["relationships"].append({**ds.tables["relationships"][2], "id": g.uid(38), "from_id": g.SUB})
    ds.tables["relationship_versions"].append({**rv(ds, g.uid(32)), "relationship_id": g.uid(38)})


def m_evidence_kind_mismatch(ds: Dataset) -> None:
    ds.tables["fact_assertions"][0]["evidence_kind"] = "human_assertion"


def m_evidence_after_assertion(ds: Dataset) -> None:
    _second_revision(ds)
    ds.tables["evidence"][0]["change_revision"] = 2


def m_secret_attribute(ds: Dataset) -> None:
    ev(ds, g.RG)["attributes"]["client_secret"] = "x"  # noqa: S105 - a forbidden attribute name is the test


def m_missing_required(ds: Dataset) -> None:
    del ev(ds, g.RG)["attributes"]["location"]


def m_bad_enum(ds: Dataset) -> None:
    ev(ds, g.WEB)["attributes"]["site_kind"] = "spaceship"


def m_unknown_type(ds: Dataset) -> None:
    ev(ds, g.TEAM)["type_key"] = "core.nothing"


def m_unknown_taxonomy(ds: Dataset) -> None:
    ev(ds, g.TEAM)["taxonomy_version"] = "9.9.9"


def m_wrong_category(ds: Dataset) -> None:
    ev(ds, g.TEAM)["category"] = "scope"


def m_logical_with_provider(ds: Dataset) -> None:
    ev(ds, g.TEAM)["provider"] = "azure"


def m_physical_without_native_type(ds: Dataset) -> None:
    ev(ds, g.RG)["native_type"] = None


def m_wrong_fixed_native_type(ds: Dataset) -> None:
    ev(ds, g.RG)["native_type"] = "microsoft.web/sites"


def m_wrong_provider(ds: Dataset) -> None:
    ev(ds, g.RG)["provider"] = "aws"


def m_environment_scope_on_neutral(ds: Dataset) -> None:
    ev(ds, g.TEAM)["environment_scope"] = "unknown"


def m_scope_required(ds: Dataset) -> None:
    v = ev(ds, g.WEB)
    v["environment_scope"], v["environment_id"] = None, None


def m_environment_not_environment(ds: Dataset) -> None:
    ev(ds, g.WEB)["environment_id"] = g.TEAM


def m_shared_without_membership(ds: Dataset) -> None:
    v = ev(ds, g.WEB)
    v["environment_scope"], v["environment_id"] = "shared", None


def m_namespace_not_allowed(ds: Dataset) -> None:
    ds.tables["external_identities"][1]["entity_id"] = g.TEAM  # azure.tenant_id on a team


def m_pair_not_allowed(ds: Dataset) -> None:
    ds.tables["relationships"][3]["rel_type"] = "READS_FROM"


def m_reserved_type(ds: Dataset) -> None:
    ds.tables["relationships"][4]["rel_type"] = "PROTECTS"


def m_evidence_kind_not_allowed(ds: Dataset) -> None:
    for a in ds.tables["fact_assertions"]:
        if a["fact_id"] == g.uid(34):
            a["evidence_id"], a["evidence_kind"] = g.EV_PROV, "provider_configuration"


def m_wrong_flag(ds: Dataset) -> None:
    rv(ds, g.uid(33))["environment_id"] = None


def m_containment_against_arm(ds: Dataset) -> None:
    for i in ds.tables["external_identities"]:
        if i["entity_id"] == g.RG:
            i["normalized_key"] = f"/subscriptions/{g.TENANT_GUID}/resourcegroups/rg-claims"
            i["original_key"] = i["normalized_key"]
            i["key_parts"]["subscription_id"] = g.TENANT_GUID


def m_container_mismatch(ds: Dataset) -> None:
    ev(ds, g.WEB)["container_id"] = g.SUB


def m_retype_by_import(ds: Dataset) -> None:
    _retype(ds, "import")


def m_retype_by_migration(ds: Dataset) -> None:
    _retype(ds, "migration")


def m_actor_kind_mismatch(ds: Dataset) -> None:
    ds.tables["change_sets"][0]["actor"]["namespace"] = "oidc"


def m_human_fixture_load(ds: Dataset) -> None:
    ds.tables["change_sets"][0]["actor"] = {
        "kind": "human",
        "namespace": "oidc",
        "key": g.STEWARD["key"],
        "label": None,
    }


def m_migration_kind_without_channel(ds: Dataset) -> None:
    ds.tables["change_sets"][0]["kind"] = "migration"


def m_trust_mismatch(ds: Dataset) -> None:
    ev(ds, g.TEAM)["trust_state"] = "discovered"


def m_logical_from_provider_only(ds: Dataset) -> None:
    for a in ds.tables["fact_assertions"]:
        if a["fact_id"] == g.TEAM:
            a["evidence_id"], a["evidence_kind"] = g.EV_PROV, "provider_configuration"


def m_verified_by_service(ds: Dataset) -> None:
    ev(ds, g.WEB)["verified_by"] = {"namespace": "atlas.service", "key": "import-worker"}


def m_conflict_value_stored(ds: Dataset) -> None:
    for n, value in ((80, "westeurope"), (81, "northeurope")):
        ds.tables["fact_assertions"].append(
            {
                "org_id": g.ORG,
                "id": g.uid(n),
                "fact_kind": "entity",
                "fact_id": g.RG,
                "field_path": "attributes.location",
                "asserted_value": value,
                "evidence_id": g.EV_PROV,
                "evidence_kind": "provider_configuration",
                "revision_from": 1,
                "revision_to": None,
            }
        )
    ev(ds, g.RG)["trust_state"] = "conflicted"


def m_depends_on_discovered(ds: Dataset) -> None:
    r = ds.tables["relationships"][4]
    r["rel_type"] = "DEPENDS_ON"
    rv(ds, r["id"])["trust_state"] = "discovered"


def m_revert_mismatch(ds: Dataset) -> None:
    _second_revision(ds)
    _second_revision(ds)
    ds.tables["change_sets"][-1].update(revision=3, kind="revert", reverts_revision=2, channel="api")
    old = ev(ds, g.TEAM)
    old["revision_to"] = 2
    ds.tables["entity_versions"].append({**old, "revision_from": 2, "revision_to": None, "display_name": "Renamed"})


MUTATIONS: list[tuple[Mutation, str]] = [
    (m_cross_org_reference, "H-04"),
    (m_unknown_org, "H-04"),
    (m_revision_without_change_set, "H-05"),
    (m_two_current_versions, "H-06"),
    (m_duplicate_ids, "H-02"),
    (m_identity_bound_twice, "H-07"),
    (m_unnormalized_key, "H-08"),
    (m_invalid_key, "H-08"),
    (m_duplicate_relationship, "H-09"),
    (m_self_relationship, "H-09"),
    (m_dangling, "H-10"),
    (m_two_parents, "H-14"),
    (m_evidence_kind_mismatch, "H-12"),
    (m_evidence_after_assertion, "H-12"),
    (m_secret_attribute, "H-13"),
    (m_actor_kind_mismatch, "H-11"),
    (m_missing_required, "D-01"),
    (m_bad_enum, "D-01"),
    (m_unknown_type, "D-01"),
    (m_unknown_taxonomy, "D-01"),
    (m_wrong_category, "D-02"),
    (m_logical_with_provider, "D-02"),
    (m_physical_without_native_type, "D-02"),
    (m_wrong_fixed_native_type, "D-02"),
    (m_wrong_provider, "D-02"),
    (m_environment_scope_on_neutral, "D-03"),
    (m_scope_required, "D-03"),
    (m_environment_not_environment, "D-03"),
    (m_shared_without_membership, "D-03"),
    (m_pair_not_allowed, "D-04"),
    (m_reserved_type, "D-04"),
    (m_evidence_kind_not_allowed, "D-05"),
    (m_containment_against_arm, "D-06"),
    (m_wrong_flag, "D-08"),
    (m_container_mismatch, "D-09"),
    (m_retype_by_import, "D-10"),
    (m_retype_by_migration, "D-10"),
    (m_depends_on_discovered, "D-11"),
    (m_trust_mismatch, "D-12"),
    (m_logical_from_provider_only, "D-12"),
    (m_human_fixture_load, "D-13"),
    (m_migration_kind_without_channel, "D-13"),
    (m_revert_mismatch, "D-14"),
    (m_namespace_not_allowed, "D-15"),
    (m_verified_by_service, "D-19"),
    (m_conflict_value_stored, "D-20"),
]


@pytest.mark.parametrize(("mutate", "rule"), MUTATIONS, ids=[m.__name__ for m, _ in MUTATIONS])
def test_each_rule_fires(mutate: Mutation, rule: str) -> None:
    ds = g.build()
    mutate(ds)
    assert rule in found(ds)


def test_scoped_validation_checks_only_the_revision_in_scope() -> None:
    ds = g.build()
    m_trust_mismatch(ds)
    _second_revision(ds)
    assert "D-12" not in found(ds, {g.ORG: {2}})  # revision 1 rows are not re-checked when publishing revision 2
    assert "D-12" in found(ds, {g.ORG: {1}})


def test_unresolved_required_field_is_accepted_only_while_in_conflict() -> None:
    ds = g.build()
    m_conflict_value_stored(ds)
    del ev(ds, g.RG)["attributes"]["location"]
    assert found(ds) == set()  # I-6: unresolved, not missing
    ds.tables["fact_assertions"] = [a for a in ds.tables["fact_assertions"] if a["asserted_value"] != "northeurope"]
    assert "D-01" in found(ds)


def test_automatic_retyping_is_allowed_in_any_change_set() -> None:
    tax = taxonomy.current()
    a = {"type_key": "infra.data_store", "provider": "azure", "native_type": "microsoft.sql/servers/databases"}
    b = {"type_key": "azure.sql_database", "provider": "azure", "native_type": "microsoft.sql/servers/databases"}
    assert rules.is_automatic_retype(tax, a, b)
    assert not rules.is_automatic_retype(tax, {**a, "provider": "aws"}, b)


def test_finding_renders_its_class_and_rule() -> None:
    assert str(rules.Finding("D-12", rules.DOMAIN, "x")) == "[DOMAIN] D-12: x"


def test_d19_checks_a_verification_when_recorded_not_when_carried_forward() -> None:
    ds = g.build()
    ev(ds, g.WEB)["expires_at"] = "2026-10-05T00:00:00Z"
    _second_revision(ds, kind="migration", actor_kind="service")
    ds.tables["change_sets"][-1].update(channel="migration", created_at="2026-10-06T00:00:00Z")
    old = ev(ds, g.WEB)
    old["revision_to"] = 2
    ds.tables["entity_versions"].append({**old, "revision_from": 2, "revision_to": None, "trust_state": "stale"})
    assert found(ds, {g.ORG: {2}}) == set()  # expired and carried forward: stale, no D-19
    g.current(ds, "entity_versions", entity_id=g.WEB)["verified_at"] = "2026-10-06T00:00:00Z"  # a new record now
    assert "D-19" in found(ds, {g.ORG: {2}})
