"""Row mapping and the publishing plan (diff, derived fields, rejections) without a database.

The database half of the procedure (counter lock, writes, triggers, privileges, integrity SQL) is covered by
``tests/integration/test_canonical_publish.py`` against PostgreSQL 15.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from atlas.domain.canonical import rules
from atlas.persistence.canonical import rows
from atlas.persistence.canonical.publish import (
    AssertionInput,
    EntityInput,
    EvidenceInput,
    IdentityInput,
    PublishRejected,
    RelationshipInput,
    _Plan,
)
from atlas.persistence.canonical.repository import OrgScope
from tests.unit import canonical_graph as g

pytestmark = pytest.mark.unit
SCOPE = OrgScope(g.ORG, "default")
AT2 = "2026-10-02T09:00:00Z"


def plan() -> _Plan:
    return _Plan(SCOPE, g.build(), g.TAXES["1.1.0"], 2, AT2)


def entity_input(entity_id: str, **changes: object) -> EntityInput:
    v = g.current(g.build(), "entity_versions", entity_id=entity_id)
    fields = {
        k: v[k]
        for k in (
            "type_key",
            "display_name",
            "lifecycle",
            "record_state",
            "merged_into",
            "provider",
            "native_type",
            "environment_scope",
            "environment_id",
            "container_id",
            "attributes",
        )
    }
    verification = {k: v[k] for k in ("verification_state", "verified_by", "verified_at", "expires_at")}
    return EntityInput(entity_id=entity_id, **{**fields, **changes}, verification=verification)


def finish(p: _Plan) -> set[str]:
    """Run the procedure's validation step on the plan, as publish() does."""
    p.ds.tables["change_sets"].append(
        {
            **p.ds.tables["change_sets"][0],
            "revision": 2,
            "kind": "steward_edit",
            "channel": "api",
            "created_at": AT2,
            "actor": {"kind": "human", "namespace": "oidc", "key": g.STEWARD["key"], "label": None},
        }
    )
    return {f.rule for f in rules.blocking(rules.validate(p.ds, g.TAXES, {g.ORG: {2}}))}


# --------------------------------------------------------------------------- rows


def test_timestamps_round_trip_in_the_canonical_format() -> None:
    assert rows.format_ts(rows.parse_ts("2026-10-01T09:00:00Z")) == "2026-10-01T09:00:00Z"
    assert rows.format_ts(datetime(2026, 10, 1, 9, 0, 0, 123, tzinfo=UTC)) == "2026-10-01T09:00:00.000123Z"
    assert rows.parse_ts(None) is None
    assert rows.format_ts(None) is None


def test_change_set_and_versions_split_actor_columns() -> None:
    ds = g.build()
    cs = ds.tables["change_sets"][0]
    params = dict(zip(rows.COLUMNS["change_set"], rows.to_db("change_set", cs), strict=True))
    assert (params["actor_kind"], params["actor_namespace"], params["actor_key"]) == (
        "service",
        "atlas.service",
        "fixture-loader",
    )
    back = rows.from_db("change_set", params)
    assert back["actor"] == cs["actor"]
    assert back["created_at"] == cs["created_at"]
    web = g.current(ds, "entity_versions", entity_id=g.WEB)
    params = dict(zip(rows.COLUMNS["entity_version"], rows.to_db("entity_version", web), strict=True))
    assert params["verified_by_namespace"] == "oidc"
    assert isinstance(params["attributes"], Jsonb)
    params["attributes"] = web["attributes"]
    assert rows.from_db("entity_version", params) == web


def test_fact_assertions_carry_their_typed_fact_reference() -> None:
    ds = g.build()
    rel = next(a for a in ds.tables["fact_assertions"] if a["fact_kind"] == "relationship")
    params = dict(
        zip(
            rows.COLUMNS["fact_assertion"],
            rows.to_db("fact_assertion", rel, rel_types={rel["fact_id"]: "OWNS"}),
            strict=True,
        )
    )
    assert (params["entity_fact_id"], params["relationship_fact_id"], params["rel_type"]) == (
        None,
        rel["fact_id"],
        "OWNS",
    )
    assert params["asserted_value"] is None  # existence assertions store SQL NULL
    field = {**rel, "fact_kind": "entity", "field_path": "display_name", "asserted_value": "x"}
    params = dict(zip(rows.COLUMNS["fact_assertion"], rows.to_db("fact_assertion", field), strict=True))
    assert params["entity_fact_id"] == rel["fact_id"]
    assert isinstance(params["asserted_value"], Jsonb)
    params["asserted_value"] = "x"
    assert rows.from_db("fact_assertion", params) == field
    assert rows.insert_sql("entity").startswith("INSERT INTO atlas.entity (org_id, id, created_revision) VALUES")


# --------------------------------------------------------------------------- plan


def test_an_unchanged_draft_is_empty() -> None:
    p = plan()
    assert p.entity_rows([entity_input(g.TEAM), entity_input(g.WEB)]) == set()
    p.identity_rows([IdentityInput(g.uid(70), g.APP, "org.key", "APP-1", "org_key")], [])
    assert p.empty
    assert not p.findings


def test_a_rename_closes_one_version_and_keeps_the_trust_state() -> None:
    p = plan()
    changed = p.entity_rows([entity_input(g.TEAM, display_name="Claims Platform Team")])
    p.derive(changed, set(), set(), {})
    assert [v["revision_to"] for v in p.closed["entity_versions"]] == [2]
    (new,) = p.new["entity_versions"]
    assert (new["display_name"], new["revision_from"], new["trust_state"], new["taxonomy_version"]) == (
        "Claims Platform Team",
        2,
        "documented",
        "1.1.0",
    )
    assert finish(p) == set()


def test_a_new_entity_gets_its_trust_from_its_evidence() -> None:
    p = plan()
    new_id = g.uid(20)
    p.evidence_rows(
        [EvidenceInput(g.uid(93), "contract", "IaC manifest", "import:iac-template:v1", "2026-10-01T00:00:00Z")]
    )
    changed = p.entity_rows([EntityInput(new_id, "core.service", "Quote Service", container_id=None)])
    asserted = p.assertion_rows([AssertionInput(g.uid(94), "entity", new_id, g.uid(93))], [])
    p.derive(changed, set(), asserted, {new_id: "discovered"})
    assert p.current_ev[new_id]["trust_state"] == "documented"  # IaC is declared evidence (G2 closure decision 1)
    assert {f.rule for f in p.findings} == {"D-12"}  # the expected state differs from the trust function


def test_provider_configuration_never_establishes_a_logical_fact() -> None:
    p = plan()
    new_id = g.uid(21)
    changed = p.entity_rows([EntityInput(new_id, "core.service", "Tagged Service")])
    asserted = p.assertion_rows([AssertionInput(g.uid(95), "entity", new_id, g.EV_PROV)], [])
    p.derive(changed, set(), asserted, {})
    assert "D-12" in finish(p)  # TF-1: no applicable evidence, so nothing may be published


def test_moving_an_endpoint_rederives_relationship_flags() -> None:
    p = plan()
    uat = g.uid(22)
    env = g.current(g.build(), "entity_versions", entity_id=g.ENV)
    changed = p.entity_rows(
        [
            EntityInput(uat, "core.environment", "UAT", attributes={"environment_kind": "uat"}),
            entity_input(g.WEB, environment_id=uat),
        ]
    )
    asserted = p.assertion_rows([AssertionInput(g.uid(96), "entity", uat, g.EV_DOC)], [])
    p.derive(changed, set(), asserted, {})
    deployed = p.current_rv[g.uid(33)]
    assert deployed["environment_id"] == uat
    assert deployed["revision_from"] == 2
    assert env["entity_id"] == g.ENV
    assert finish(p) == set()


def test_a_removal_is_trusted_like_its_removal_evidence() -> None:
    p = plan()
    owns = RelationshipInput(g.uid(34), "OWNS", g.TEAM, g.APP, record_state="removed")
    changed = p.relationship_rows([owns])
    asserted = p.assertion_rows(
        [AssertionInput(g.uid(97), "relationship", g.uid(34), g.EV_HUMAN, "record_state", "removed")], []
    )
    p.derive(set(), changed, asserted, {})
    assert p.current_rv[g.uid(34)]["record_state"] == "removed"
    assert p.current_rv[g.uid(34)]["trust_state"] == "curated"
    assert finish(p) == set()


def test_relationship_identity_is_immutable_and_unique() -> None:
    p = plan()
    p.relationship_rows([RelationshipInput(g.uid(34), "OWNS", g.TEAM, g.WEB)])  # endpoint changed
    p.relationship_rows([RelationshipInput(g.uid(40), "OWNS", g.TEAM, g.APP)])  # same fact, new ID
    assert [f.rule for f in p.findings] == ["H-09", "H-09"]


def test_identity_evidence_and_assertion_rejections() -> None:
    p = plan()
    p.identity_rows([IdentityInput(g.uid(76), g.WEB, "azure.arm", "not-an-arm-id", "provider_id")], [g.uid(999)])
    p.evidence_rows([EvidenceInput(g.EV_DOC, "documentation", "changed", "import:document:v1", "2026-09-01T12:00:00Z")])
    p.assertion_rows([AssertionInput(g.uid(98), "entity", g.TEAM, g.uid(998))], [g.uid(997)])
    assert sorted(f.rule for f in p.findings) == ["H-02", "H-04", "H-04", "H-04", "H-08"]
    with pytest.raises(PublishRejected) as caught:
        raise PublishRejected(p.findings)
    assert caught.value.rule_ids == {"H-02", "H-04", "H-08"}


def test_closing_and_rebinding_identities_and_assertions() -> None:
    p = plan()
    team_assertion = next(a for a in p.ds.tables["fact_assertions"] if a["fact_id"] == g.TEAM)
    p.identity_rows([IdentityInput(g.uid(77), g.TEAM, "org.key", "TEAM-1", "org_key")], [g.uid(70)])
    touched = p.assertion_rows([AssertionInput(g.uid(99), "entity", g.TEAM, g.EV_HUMAN)], [team_assertion["id"]])
    p.derive(set(), set(), touched, {})
    assert p.current_ev[g.TEAM]["trust_state"] == "curated"  # now known from the steward, not the document
    assert len(p.closed["external_identities"]) == 1
    assert len(p.closed["fact_assertions"]) == 1
    assert finish(p) == set()  # an identity binding is optional; closing one is a normal change


def test_plan_works_on_a_copy() -> None:
    base = g.build()
    before = copy.deepcopy(base.tables)
    p = _Plan(SCOPE, base, g.TAXES["1.1.0"], 2, AT2)
    p.entity_rows([entity_input(g.TEAM, display_name="Renamed")])
    assert base.tables == before


def test_settings_urls_become_libpq_urls() -> None:
    from atlas.persistence.canonical.repository import libpq_url

    assert libpq_url("postgresql+psycopg://u@h:5432/atlas_test") == "postgresql://u@h:5432/atlas_test"
    assert libpq_url("postgresql://u@h/db") == "postgresql://u@h/db"
