"""Canonical domain building blocks: packaged taxonomy and seed, identity normalizers, trust function, graph helpers.

The full oracles (the pinned taxonomy artifact, the 36-case truth table, the golden fixture) run in the pinned-specs
suite (``tests/contract/test_pinned_canonical.py``); these cases cover the code without them.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlas.domain.canonical import taxonomy
from atlas.domain.canonical.graph import check_pair, discriminator_of, relationship_flags, sha256_of, valid_at
from atlas.domain.canonical.identity import NormalizationError, normalize
from atlas.domain.canonical.trust import NO_POLICY, Claim, evaluate, freshness_of

pytestmark = pytest.mark.unit
TAX = taxonomy.current()
SUB = "00000000-0000-4000-8000-00000000a001"


# --------------------------------------------------------------------------- taxonomy


def test_packaged_taxonomy_is_the_approved_version() -> None:
    assert TAX.version == taxonomy.CURRENT_VERSION == "1.1.0"
    assert TAX.status == "accepted"
    assert len(TAX.types) == 38
    assert "contract" in TAX.rels["CONTAINS"]["allowed_evidence_kinds"]  # U-1 (taxonomy 1.1.0)
    with pytest.raises(FileNotFoundError):
        taxonomy.packaged("0.0.1")


def test_overlay_extends_only_its_base() -> None:
    overlay = {
        "taxonomy_version": "1.2.0-test.1",
        "extends": "1.1.0",
        "status": "synthetic_test_only",
        "add_native_type_mappings": {"gcp": [{"native_type": "x.googleapis.com/Thing", "category": "data_store"}]},
    }
    derived = TAX.with_overlay(overlay)
    assert derived.version == "1.2.0-test.1"
    assert derived.mappings["gcp"]["x.googleapis.com/Thing"] == "data_store"
    assert "x.googleapis.com/Thing" not in TAX.mappings.get("gcp", {})
    with pytest.raises(ValueError, match="extends"):
        TAX.with_overlay({**overlay, "extends": "9.9.9"})


def test_type_matching() -> None:
    assert TAX.matches("azure.sql_database", ["category:data_store"])
    assert TAX.matches("azure.sql_database", ["category:physical"])
    assert TAX.matches("core.team", ["*"])
    assert not TAX.matches("core.team", ["category:physical"])
    assert not TAX.matches("core.nothing", ["*"])
    assert TAX.allowed_parent("azure.sql_database", "azure.sql_server")
    assert not TAX.allowed_parent("azure.sql_database", "azure.resource_group")
    assert TAX.enum_values("@deployment_roles") == TAX.enums["deployment_roles"]
    assert TAX.enum_values(["a"]) == ["a"]


def test_seed_rows_cover_every_reference_table() -> None:
    seed = taxonomy.seed_rows(TAX)
    assert seed["taxonomy_release"] == [{"version": "1.1.0", "status": "accepted"}]
    assert len(seed["entity_type"]) == 38
    assert len(seed["relationship_type"]) == 18
    assert len(seed["provider"]) == 11
    assert len(seed["category"]) == 15
    assert {r["evidence_kind"] for r in seed["relationship_evidence_kind"] if r["rel_type"] == "DEPLOYED_AS"} == {
        "provider_configuration",
        "contract",
        "documentation",
        "human_assertion",
    }
    rules = {(r["rel_type"], r["from_type"], r["to_type"]) for r in seed["relationship_rule"]}
    assert ("CONTAINS", "azure.sql_server", "azure.sql_database") in rules
    assert ("DEPLOYED_AS", "core.application", "azure.app_service") in rules
    assert not any(r[0] == "PROTECTS" for r in rules)  # reserved types have no pairs
    site_kind = next(
        r for r in seed["attribute_rule"] if (r["owner_key"], r["attribute"]) == ("azure.app_service", "site_kind")
    )
    assert site_kind["required"]
    assert site_kind["allowed_values"] == ["app", "functionapp", "other"]
    assert seed == taxonomy.seed_rows(TAX)  # deterministic


# --------------------------------------------------------------------------- identity (ADR-0003)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            f"/subscriptions/{SUB}/resourceGroups/RG-Claims/providers/Microsoft.Sql/servers/S1/databases/D1/",
            f"/subscriptions/{SUB}/resourcegroups/rg-claims/providers/microsoft.sql/servers/s1/databases/d1",
        ),
        (f"  /subscriptions/{SUB.upper()}  ", f"/subscriptions/{SUB}"),
        (
            "/providers/Microsoft.Management/managementGroups/MG1",
            "/providers/microsoft.management/managementgroups/mg1",
        ),
    ],
)
def test_arm_1_normalizes(original: str, expected: str) -> None:
    key = normalize(TAX, "azure.arm", original)
    assert key.normalized_key == expected
    assert key.normalization_version == "arm-1"


@pytest.mark.parametrize(
    "original",
    [
        f"/subscriptions/{SUB}//resourceGroups/rg",
        f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Sql/servers",
        f"/subscriptions/{SUB}/resourceGroups/../providers/Microsoft.Sql/servers/s1",
        "/subscriptions/not-a-guid",
        f"/subscriptions/{SUB}/resourceGroups/rg?api-version=1",
        "subscriptions/x",
        f"/subscriptions/{SUB}/resource Groups/rg",
        f"/subscriptions/{SUB}/resourceGroups",
        "/tenants/x",
        f"/subscriptions/{SUB}/resourceGroups/rg/x/y",
        "/" + "a" * 2050,
    ],
)
def test_arm_1_rejects_never_repairs(original: str) -> None:
    with pytest.raises(NormalizationError):
        normalize(TAX, "azure.arm", original)


def test_arm_key_parts() -> None:
    key = normalize(TAX, "azure.arm", f"/subscriptions/{SUB}/resourceGroups/RG/providers/Microsoft.Sql/servers/S1")
    assert key.key_parts == {
        "subscription_id": SUB,
        "resource_group": "rg",
        "provider_namespace": "microsoft.sql",
        "type_path": "servers",
        "name_path": "s1",
    }


def test_validation_only_namespaces_preserve_case() -> None:
    arn = "arn:aws:s3:::Example-Bucket"
    assert normalize(TAX, "aws.arn", arn).normalized_key == arn
    ocid = " ocid1.instance.oc1.eu-frankfurt-1.abc123 "
    assert normalize(TAX, "oci.ocid", ocid).normalized_key == ocid.strip()
    with pytest.raises(NormalizationError):
        normalize(TAX, "aws.arn", "arn:aws:s3")


@pytest.mark.parametrize(
    "namespace", ["huawei.resource", "tencent.qcs", "ibm.crn", "apim.operation", "unknown.ns", "source.Bad"]
)
def test_planned_reserved_unknown_and_invalid_namespaces_are_rejected(namespace: str) -> None:
    with pytest.raises(NormalizationError):
        normalize(TAX, namespace, "anything")


def test_k8s_uid_apim_guid_org_key_and_source() -> None:
    uid = "7A1C0000-0000-4000-8000-000000000001"
    cluster = f"/subscriptions/{SUB}/resourceGroups/RG/providers/Microsoft.ContainerService/managedClusters/AKS"
    key = normalize(TAX, "k8s.uid", f"azure.arm:{cluster}#{uid}")
    assert key.normalized_key == f"azure.arm:{cluster.lower()}#{uid.lower()}"
    assert key.key_parts == {"cluster_namespace": "azure.arm", "cluster_key": cluster.lower()}
    for bad in (f"azure.arm:{cluster}", f"k8s.uid:x#{uid}", f"azure.arm:{cluster}#not-a-uid"):
        with pytest.raises(NormalizationError):
            normalize(TAX, "k8s.uid", bad)
    apim = normalize(
        TAX,
        "apim.api",
        f"/subscriptions/{SUB}/resourceGroups/RG/providers/Microsoft.ApiManagement/service/A#api:quotes;rev=2",
    )
    assert apim.key_parts["api_id"] == "quotes"
    assert apim.normalized_key.endswith("#api:quotes;rev=2")
    with pytest.raises(NormalizationError):
        normalize(TAX, "apim.api", "/x#api:")
    assert normalize(TAX, "azure.tenant_id", SUB.upper()).normalized_key == SUB
    with pytest.raises(NormalizationError):
        normalize(TAX, "azure.tenant_id", "not-a-guid")
    assert normalize(TAX, "org.key", " APP-0042 ").normalized_key == "app-0042"
    with pytest.raises(NormalizationError):
        normalize(TAX, "org.key", "   ")
    assert normalize(TAX, "source.cmdb", "CI-0001").normalized_key == "CI-0001"


# --------------------------------------------------------------------------- trust function v1


def a(
    kind: str,
    *,
    field: str | None = None,
    value: Any = None,
    method: str = "m",
    observed: str = "2026-10-01T00:00:00Z",
    valid_to: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_kind": kind,
        "field_path": field,
        "asserted_value": value,
        "method": method,
        "observed_at": observed,
        "valid_to": valid_to,
    }


AT = "2026-10-08T09:00:00Z"
PHYSICAL = Claim("entity", physical=True)
LOGICAL = Claim("entity", physical=False)
VERIFIED = {
    "verification_state": "verified",
    "verified_by": {"namespace": "oidc", "key": "https://x|y"},
    "verified_at": "2026-10-01T00:00:00Z",
    "expires_at": None,
}


@pytest.mark.parametrize(
    ("claim", "assertions", "state", "rule"),
    [
        (PHYSICAL, [a("provider_configuration")], "discovered", "TF-4"),
        (PHYSICAL, [a("provider_configuration"), a("human_assertion")], "discovered", "TF-4"),
        (PHYSICAL, [a("human_assertion")], "curated", "TF-5"),
        (LOGICAL, [a("contract")], "documented", "TF-6"),
        (PHYSICAL, [a("inferred")], "inferred", "TF-7"),
        (LOGICAL, [a("provider_configuration")], None, "TF-1"),
        (PHYSICAL, [a("provider_configuration", valid_to="2026-10-02T00:00:00Z")], None, "TF-1"),
        (
            PHYSICAL,
            [
                a("provider_configuration"),
                a("provider_configuration", field="attributes.location", value="x"),
                a("documentation", field="attributes.location", value="y"),
            ],
            "conflicted",
            "TF-2",
        ),
        (
            Claim("relationship", False, frozenset({"provider_configuration", "documentation"})),
            [a("provider_configuration")],
            None,
            "TF-1",
        ),
    ],
)
def test_trust_rules(claim: Claim, assertions: list[dict[str, Any]], state: str | None, rule: str) -> None:
    result = evaluate(claim, assertions, None, NO_POLICY, AT)
    assert (result.trust_state, result.rule) == (state, rule)


def test_freshness_and_staleness() -> None:
    policy = {"freshness": [{"method": "doc", "stale_after_days": 365}], "precedence": []}
    old = a("documentation", method="doc", observed="2024-01-01T00:00:00Z")
    assert freshness_of(old, policy, AT) == "expired"
    assert freshness_of(a("documentation", method="doc"), policy, AT) == "fresh"
    assert freshness_of(old, NO_POLICY, AT) == "unassessed"
    assert evaluate(LOGICAL, [old], None, policy, AT).trust_state == "stale"
    assert evaluate(LOGICAL, [old], None, NO_POLICY, AT).trust_state == "documented"  # undefined: never stale
    expired = {**VERIFIED, "expires_at": "2026-10-05T00:00:00Z"}
    result = evaluate(PHYSICAL, [a("provider_configuration")], expired, NO_POLICY, AT)
    assert (result.trust_state, result.rule, result.verification_state) == ("stale", "TF-3a", "unverified")


def test_verification_needs_a_human_and_supporting_evidence() -> None:
    assert evaluate(PHYSICAL, [a("provider_configuration")], VERIFIED, NO_POLICY, AT).verification_state == "verified"
    declared = evaluate(PHYSICAL, [a("contract")], VERIFIED, NO_POLICY, AT)  # U-1: IaC never verifies alone
    assert declared.verification_state == "unverified"
    assert declared.verification_reasons
    service = {**VERIFIED, "verified_by": {"namespace": "atlas.service", "key": "x"}}
    assert evaluate(PHYSICAL, [a("provider_configuration")], service, NO_POLICY, AT).verification_reasons == [
        "verified_by is not a human actor"
    ]
    later = {**VERIFIED, "verified_at": "2026-12-01T00:00:00Z"}
    assert (
        "after the evaluation time"
        in evaluate(PHYSICAL, [a("human_assertion")], later, NO_POLICY, AT).verification_reasons[0]
    )
    assert evaluate(LOGICAL, [a("documentation")], VERIFIED, NO_POLICY, AT).verification_state == "verified"


def test_conflict_keeps_the_prior_value_and_drafts_are_proposed() -> None:
    both = [
        a("provider_configuration"),
        a("provider_configuration", field="attributes.location", value="x"),
        a("provider_configuration", field="attributes.location", value="y"),
    ]
    assert evaluate(PHYSICAL, both, None, NO_POLICY, AT).selected == {"attributes.location": None}
    prior = evaluate(PHYSICAL, both, None, NO_POLICY, AT, {"attributes.location": "x"})
    assert prior.selected == {"attributes.location": "x"}
    assert prior.conflicts == ["attributes.location"]
    assert evaluate(LOGICAL, [a("documentation")], None, NO_POLICY, AT, origin="draft").trust_state == "proposed"
    with pytest.raises(ValueError, match="precedence"):
        evaluate(PHYSICAL, both, None, {"freshness": [], "precedence": [{"x": 1}]}, AT)


# --------------------------------------------------------------------------- graph helpers


def version(**kw: Any) -> dict[str, Any]:
    return {
        "type_key": "azure.app_service",
        "provider": "azure",
        "environment_scope": "single",
        "environment_id": "E1",
        **kw,
    }


def test_relationship_flags() -> None:
    assert relationship_flags(version(), version())["environment_id"] == "E1"
    cross = relationship_flags(version(), version(environment_id="E2"))
    assert cross["cross_environment"]
    assert cross["environment_id"] is None
    assert relationship_flags(version(environment_scope="shared", environment_id=None), version())["shared_endpoint"]
    assert relationship_flags(version(), version(provider="aws"))["cross_provider"]


def test_check_pair_reasons() -> None:
    app = {"type_key": "core.application", "provider": None}
    web = version()
    team = {"type_key": "core.team", "provider": None}
    assert check_pair(TAX, "DEPLOYED_AS", app, web, {"deployment_role": "primary"}) == []
    assert check_pair(TAX, "DEPLOYED_AS", app, web, {"deployment_role": "boss"})[0].endswith("not in the vocabulary")
    assert check_pair(TAX, "DEPLOYED_AS", app, web, {"colour": "red"}) == ["DEPLOYED_AS has no attribute colour"]
    assert check_pair(TAX, "NOPE", app, web, {}) == ["unknown relationship type NOPE"]
    assert "reserved" in check_pair(TAX, "PROTECTS", app, web, {})[0]
    assert check_pair(TAX, "OWNS", team, {"type_key": "core.x"}, {}) == ["unknown endpoint type core.x"]
    assert "requires attribute" in " ".join(
        check_pair(TAX, "SUPPORTS_CAPABILITY", app, {"type_key": "core.capability"}, {})
    )
    assert check_pair(TAX, "CONTAINS", app, web, {}) == [
        "core.application is not an allowed CONTAINS parent of azure.app_service",
        "containment never crosses logical and physical families",
    ]
    rg = {"type_key": "azure.resource_group", "provider": "azure"}
    assert check_pair(TAX, "CONTAINS", rg, {**web, "provider": "aws"}, {}) == [
        "physical containment stays within one provider"
    ]
    assert (
        "CONNECTS_TO is not allowed" in check_pair(TAX, "CONNECTS_TO", web, web, {"connectivity_kind": "teleport"})[0]
    )
    assert discriminator_of(TAX, "CONNECTS_TO", {"connectivity_kind": "private_link"}) == "private_link"
    assert discriminator_of(TAX, "OWNS", {}) == ""
    assert discriminator_of(TAX, "NOPE", {}) == ""


def test_hash_and_interval_helpers() -> None:
    assert sha256_of(b"x") == sha256_of(b"x") != sha256_of({"x": 1})
    assert valid_at({"revision_from": 1, "revision_to": None}, 5)
    assert not valid_at({"revision_from": 1, "revision_to": 3}, 3)
