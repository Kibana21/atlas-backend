"""A tiny, valid synthetic canonical graph for unit tests (no database, no pinned specs needed).

One organization, revision 1 (a fixture load by a service): an environment, a team, an application, an Azure
tenant -> subscription -> resource group -> app service chain with ARM identities, and the relationships that
connect them, each with evidence. Rule tests mutate a copy and expect one specific finding. The golden fixture
(pinned specs tests) is the full oracle; this graph exists so unit tests cover every rule without it.
"""

from __future__ import annotations

import copy
from typing import Any

from atlas.domain.canonical import taxonomy
from atlas.domain.canonical.graph import Dataset, relationship_flags
from atlas.domain.canonical.identity import normalize

ORG = "0190f000-0000-7000-8000-000000000001"
SUB_GUID = "00000000-0000-4000-8000-00000000a001"
TENANT_GUID = "00000000-0000-4000-8000-00000000b001"
AT = "2026-10-01T09:00:00Z"
STEWARD = {"namespace": "oidc", "key": "https://login.example.invalid/synthetic|steward-1"}


def uid(n: int) -> str:
    return f"0190f000-0000-7000-8000-{n:012d}"


ENV, TEAM, APP, TENANT, SUB, RG, WEB = (uid(n) for n in range(10, 17))
ARM_SUB = f"/subscriptions/{SUB_GUID}"
ARM_RG = f"{ARM_SUB}/resourceGroups/rg-claims"
ARM_WEB = f"{ARM_RG}/providers/Microsoft.Web/sites/web-claims"
EV_DOC, EV_PROV, EV_HUMAN = uid(90), uid(91), uid(92)

TAXES = {taxonomy.CURRENT_VERSION: taxonomy.current()}


def _version(entity_id: str, type_key: str, name: str, trust: str, **extra: Any) -> dict[str, Any]:
    t = taxonomy.current().types[type_key]
    return {
        "org_id": ORG,
        "entity_id": entity_id,
        "revision_from": 1,
        "revision_to": None,
        "type_key": type_key,
        "category": t["category"],
        "provider": None,
        "native_type": None,
        "record_state": "present",
        "merged_into": None,
        "display_name": name,
        "lifecycle": "active",
        "environment_scope": None,
        "environment_id": None,
        "container_id": None,
        "attributes": {},
        "trust_state": trust,
        "verification_state": "unverified",
        "verified_by": None,
        "verified_at": None,
        "expires_at": None,
        "taxonomy_version": taxonomy.CURRENT_VERSION,
        **extra,
    }


def _azure(entity_id: str, type_key: str, name: str, **extra: Any) -> dict[str, Any]:
    native = taxonomy.current().types[type_key].get("native_type")
    return _version(entity_id, type_key, name, "discovered", provider="azure", native_type=native, **extra)


def _identity(n: int, entity_id: str, namespace: str, original: str) -> dict[str, Any]:
    key = normalize(taxonomy.current(), namespace, original)
    return {
        "org_id": ORG,
        "id": uid(n),
        "entity_id": entity_id,
        "namespace": namespace,
        "original_key": original,
        "normalized_key": key.normalized_key,
        "normalization_version": key.normalization_version,
        "key_parts": key.key_parts,
        "match_method": "org_key" if namespace == "org.key" else "provider_id",
        "revision_from": 1,
        "revision_to": None,
    }


def _evidence(evidence_id: str, kind: str, method: str, observed: str) -> dict[str, Any]:
    return {
        "org_id": ORG,
        "id": evidence_id,
        "kind": kind,
        "source_ref": f"synthetic {kind}",
        "artifact_ref": None,
        "artifact_hash": None,
        "method": method,
        "confidence": 1.0,
        "observed_at": observed,
        "valid_from": None,
        "valid_to": None,
        "change_revision": 1,
    }


def build() -> Dataset:
    versions = [
        _version(ENV, "core.environment", "PROD", "documented", attributes={"environment_kind": "prod"}),
        _version(TEAM, "core.team", "Claims Team", "documented"),
        _version(APP, "core.application", "Claims Portal", "documented", attributes={"org_key": "APP-1"}),
        _azure(TENANT, "azure.tenant", "Example tenant"),
        _azure(SUB, "azure.subscription", "sub-claims", container_id=TENANT),
        _azure(RG, "azure.resource_group", "rg-claims", container_id=SUB, attributes={"location": "westeurope"}),
        _azure(
            WEB,
            "azure.app_service",
            "web-claims",
            container_id=RG,
            environment_scope="single",
            environment_id=ENV,
            attributes={"location": "westeurope", "site_kind": "app"},
            verification_state="verified",
            verified_by=STEWARD,
            verified_at="2026-09-20T10:00:00Z",
            expires_at="2099-03-20T10:00:00Z",  # far future: integration tests evaluate at server time,
        ),
    ]
    by_id = {v["entity_id"]: v for v in versions}
    rels = [
        (uid(30), "CONTAINS", TENANT, SUB, {}, EV_PROV, "discovered"),
        (uid(31), "CONTAINS", SUB, RG, {}, EV_PROV, "discovered"),
        (uid(32), "CONTAINS", RG, WEB, {}, EV_PROV, "discovered"),
        (uid(33), "DEPLOYED_AS", APP, WEB, {"deployment_role": "primary"}, EV_PROV, "discovered"),
        (uid(34), "OWNS", TEAM, APP, {}, EV_DOC, "documented"),
    ]
    relationships, rel_versions, assertions = [], [], []
    for rid, rel_type, frm, to, attrs, _ev, trust in rels:
        relationships.append(
            {
                "org_id": ORG,
                "id": rid,
                "rel_type": rel_type,
                "from_id": frm,
                "to_id": to,
                "discriminator": "",
                "created_revision": 1,
            }
        )
        rel_versions.append(
            {
                "org_id": ORG,
                "relationship_id": rid,
                "revision_from": 1,
                "revision_to": None,
                "record_state": "present",
                "lifecycle": "active",
                **relationship_flags(by_id[frm], by_id[to]),
                "attributes": attrs,
                "trust_state": trust,
                "verification_state": "unverified",
                "verified_by": None,
                "verified_at": None,
                "expires_at": None,
                "taxonomy_version": taxonomy.CURRENT_VERSION,
            }
        )
    n = 50
    for v in versions:
        n += 1
        ev = EV_PROV if v["trust_state"] == "discovered" else EV_DOC
        assertions.append(
            _assertion(n, "entity", v["entity_id"], ev, "provider_configuration" if ev == EV_PROV else "documentation")
        )
    for rid, _t, _f, _to, _a, ev, _trust in rels:
        n += 1
        assertions.append(
            _assertion(n, "relationship", rid, ev, "provider_configuration" if ev == EV_PROV else "documentation")
        )
    tables: dict[str, list[dict[str, Any]]] = {
        "organizations": [{"id": ORG, "key": "default", "display_name": "Synthetic organization"}],
        "change_sets": [
            {
                "org_id": ORG,
                "revision": 1,
                "kind": "fixture_load",
                "channel": "fixture_cli",
                "actor": {"kind": "service", "namespace": "atlas.service", "key": "fixture-loader", "label": None},
                "source_ref": "unit test",
                "input_hash": None,
                "reverts_revision": None,
                "reason": "synthetic",
                "change_reference": None,
                "created_at": AT,
                "db_role": "atlas_test_ingest",
            }
        ],
        "entities": [{"org_id": ORG, "id": v["entity_id"], "created_revision": 1} for v in versions],
        "entity_versions": versions,
        "relationships": relationships,
        "relationship_versions": rel_versions,
        "external_identities": [
            _identity(70, APP, "org.key", "APP-1"),
            _identity(71, TENANT, "azure.tenant_id", TENANT_GUID),
            _identity(72, SUB, "azure.subscription_id", SUB_GUID),
            _identity(73, SUB, "azure.arm", ARM_SUB),
            _identity(74, RG, "azure.arm", ARM_RG),
            _identity(75, WEB, "azure.arm", ARM_WEB),
        ],
        "evidence": [
            _evidence(EV_DOC, "documentation", "import:document:v1", "2026-09-01T12:00:00Z"),
            _evidence(EV_PROV, "provider_configuration", "connector:synthetic:v1", "2026-09-30T12:00:00Z"),
            _evidence(EV_HUMAN, "human_assertion", "steward:review", "2026-09-30T12:00:00Z"),
        ],
        "fact_assertions": assertions,
        "graph_snapshots": [],
    }
    return Dataset(copy.deepcopy(tables))


def _assertion(n: int, fact_kind: str, fact_id: str, evidence_id: str, kind: str) -> dict[str, Any]:
    return {
        "org_id": ORG,
        "id": uid(n),
        "fact_kind": fact_kind,
        "fact_id": fact_id,
        "field_path": None,
        "asserted_value": None,
        "evidence_id": evidence_id,
        "evidence_kind": kind,
        "revision_from": 1,
        "revision_to": None,
    }


def current(ds: Dataset, table: str, **match: Any) -> dict[str, Any]:
    return next(
        r for r in ds.tables[table] if r.get("revision_to") is None and all(r.get(k) == v for k, v in match.items())
    )
