"""CP4 draft builders without a database: revert, recorded-history replay and import drafts; the import planner."""

from __future__ import annotations

from typing import Any

import pytest

from atlas.domain.canonical.graph import Dataset, as_of
from atlas.domain.canonical.importer import plan_batch
from atlas.persistence.canonical import history
from atlas.persistence.canonical.publish import PublishRejected
from tests.unit import canonical_graph as g

pytestmark = pytest.mark.unit
TAX = g.TAXES["1.1.0"]


def with_rename(ds: Dataset) -> Dataset:
    """Revision 2 renames the team (a steward edit) and swaps its evidence."""
    ds.tables["change_sets"].append(
        {
            **ds.tables["change_sets"][0],
            "revision": 2,
            "kind": "steward_edit",
            "channel": "api",
            "created_at": "2026-10-02T09:00:00Z",
            "actor": {"kind": "human", "namespace": "oidc", "key": g.STEWARD["key"], "label": None},
        }
    )
    old = g.current(ds, "entity_versions", entity_id=g.TEAM)
    old["revision_to"] = 2
    ds.tables["entity_versions"].append(
        {**old, "revision_from": 2, "revision_to": None, "display_name": "Renamed", "trust_state": "curated"}
    )
    doc = next(a for a in ds.tables["fact_assertions"] if a["fact_id"] == g.TEAM)
    doc["revision_to"] = 2
    ds.tables["fact_assertions"].append(
        {
            **doc,
            "id": g.uid(300),
            "evidence_id": g.EV_HUMAN,
            "evidence_kind": "human_assertion",
            "revision_from": 2,
            "revision_to": None,
        }
    )
    return ds


def ids(purpose: str) -> str:
    return {"assertion": g.uid(400)}.get(purpose.split(":")[0], g.uid(401))


def test_revert_restores_the_prior_state_as_a_new_change_set() -> None:
    ds = with_rename(g.build())
    draft = history.revert_draft(ds, g.ORG, 2, 2, ids, "undo the rename")
    assert (draft.kind, draft.reverts_revision) == ("revert", 2)
    (team,) = draft.entities
    assert (team.entity_id, team.display_name) == (g.TEAM, "Claims Team")
    assert draft.close_assertions == [g.uid(300)]
    (restored,) = draft.assertions
    assert (restored.assertion_id, restored.evidence_id) == (g.uid(400), g.EV_DOC)


def test_revert_of_a_creation_removes_and_refuses_unknown_or_conflicting_revisions() -> None:
    ds = g.build()
    draft = history.revert_draft(ds, g.ORG, 1, 1, ids, "undo the load")
    assert {e.record_state for e in draft.entities} == {"removed"}
    assert {r.record_state for r in draft.relationships} == {"removed"}
    with pytest.raises(PublishRejected, match="does not exist"):
        history.revert_draft(ds, g.ORG, 3, 1, ids, "x")
    later = with_rename(g.build())
    with pytest.raises(PublishRejected) as caught:
        history.revert_draft(later, g.ORG, 1, 2, ids, "x")  # the team changed again at r2
    assert caught.value.rule_ids == {"D-14"}
    assert any("changed again after r1" in str(f) for f in caught.value.findings)


def test_recorded_drafts_carry_exactly_each_revisions_rows() -> None:
    ds = with_rename(g.build())
    recs = history.recorded_drafts(ds, g.ORG)
    assert [r.revision for r in recs] == [1, 2]
    first, second = recs
    assert (len(first.draft.entities), len(first.draft.relationships), len(first.draft.assertions)) == (7, 5, 12)
    assert first.draft.entities[0].expected_trust_state == "documented"
    assert [e.entity_id for e in second.draft.entities] == [g.TEAM]
    assert second.draft.close_assertions
    assert second.draft.assertions[0].assertion_id == g.uid(300)
    assert second.taxonomy_version == "1.1.0"
    assert all(r.draft.kind == "fixture_load" for r in recs)  # replayed by the configured loader identity


def batch(**over: Any) -> dict[str, Any]:
    b = {
        "batch_format": "atlas.import-batch.v1",
        "org_key": "default",
        "taxonomy_version": "1.1.0",
        "channel": "import",
        "source_ref": "unit",
        "reason": "unit",
        "change_reference": None,
        "evidence": [
            {
                "ref": "cfg",
                "kind": "provider_configuration",
                "source_ref": "cfg",
                "method": "export:x:v1",
                "observed_at": "2026-10-01T00:00:00Z",
                "artifact_ref": "synthetic://x",
                "artifact_hash": "sha256:" + "a" * 64,
                "confidence": 1.0,
            }
        ],
        "entities": [
            {
                "ref": "svc",
                "id": g.uid(500),
                "type_key": "core.service",
                "display_name": "Quote Service",
                "identities": [{"namespace": "org.key", "original_key": "SVC-1"}],
                "evidence_refs": ["cfg"],
                "parent": {"org_key": "APP-1"},
            }
        ],
        "relationships": [
            {
                "ref": "owns",
                "id": g.uid(501),
                "rel_type": "OWNS",
                "from": {"ref": "svc"},
                "to": {"org_key": "APP-1"},
                "evidence_refs": ["cfg"],
            }
        ],
    }
    b.update(over)
    return b


def test_import_plan_and_draft() -> None:
    ds = g.build()
    st = as_of(ds, g.ORG, 1)
    plan = plan_batch(TAX, batch(), st)
    assert [d["entity_id"] for d in plan.new_entities] == [g.uid(500)]
    assert plan.new_containments == [(g.APP, g.uid(500))]
    assert [r["ref"] for r in plan.report()] == ["owns"]  # OWNS from a service is not in the matrix: reported
    assert plan.report()[0]["rule"] == "D-04"
    draft = history.import_draft(batch(), plan, {}, history.new_id, ds)
    assert draft.kind == "import"
    assert draft.input_hash
    assert draft.input_hash.startswith("sha256:")
    assert [r.rel_type for r in draft.relationships] == ["CONTAINS"]
    reused = history.import_draft(batch(), plan, {"sha256:" + "a" * 64: g.EV_PROV}, history.new_id, ds)
    assert reused.evidence == []
    assert {a.evidence_id for a in reused.assertions} == {g.EV_PROV}


def test_import_changes_existing_facts_and_reports_every_rejection() -> None:
    ds = g.build()
    st = as_of(ds, g.ORG, 1)
    change = batch(
        entities=[
            {
                "ref": "app",
                "type_key": "core.application",
                "display_name": "Claims Portal v2",
                "identities": [{"namespace": "org.key", "original_key": "APP-1"}],
                "attributes": {"org_key": "APP-1"},
                "evidence_refs": ["cfg"],
            }
        ],
        relationships=[],
    )
    plan = plan_batch(TAX, change, st)
    assert [eid for eid, _ in plan.changed_entities] == [g.APP]
    draft = history.import_draft(change, plan, {}, history.new_id, ds)
    assert [e.display_name for e in draft.entities] == ["Claims Portal v2"]
    bad = batch(
        entities=[
            {"ref": "u", "type_key": "core.nope", "display_name": "x", "identities": [], "evidence_refs": ["cfg"]},
            {
                "ref": "k",
                "type_key": "core.service",
                "display_name": "x",
                "evidence_refs": ["cfg"],
                "identities": [{"namespace": "azure.arm", "original_key": "not-arm"}],
            },
            {"ref": "n", "type_key": "core.service", "display_name": "x", "identities": [], "evidence_refs": ["cfg"]},
            {
                "ref": "m",
                "type_key": "core.application",
                "display_name": "x",
                "identities": [],
                "evidence_refs": ["cfg"],
                "id": g.uid(502),
            },
        ],
        relationships=[
            {
                "ref": "dangling",
                "id": g.uid(503),
                "rel_type": "OWNS",
                "from": {"ref": "nobody"},
                "to": {"org_key": "APP-1"},
                "evidence_refs": ["cfg"],
            }
        ],
    )
    assert [(r["ref"], r["rule"]) for r in plan_batch(TAX, bad, st).report()] == [
        ("u", "D-01"),
        ("k", "H-08"),
        ("n", "D-18"),
        ("m", "D-01"),
        ("dangling", "H-04"),
    ]


def test_an_unchanged_batch_plans_nothing() -> None:
    ds = g.build()
    again = batch(
        entities=[
            {
                "ref": "app",
                "type_key": "core.application",
                "display_name": "Claims Portal",
                "identities": [{"namespace": "org.key", "original_key": "APP-1"}],
                "attributes": {"org_key": "APP-1"},
                "evidence_refs": ["cfg"],
            }
        ],
        relationships=[],
    )
    assert plan_batch(TAX, again, as_of(ds, g.ORG, 1)).empty
