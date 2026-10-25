"""CP4 on PostgreSQL 15 (atlas_test only): fixture loading, reconstruction, snapshots, reverts, trust recomputation,
attribution, concurrency, failure recovery and organization isolation (AC-CP4-01…06).

Organizations are created by migrations or the owner only, so the ``cp4_orgs`` fixture creates the test organizations
(and registers the golden fixture's synthetic test-only taxonomy overlay) as the migration connection and removes them
afterwards. Every canonical write runs inside ``transaction(force_rollback=True)``: nothing a test publishes survives.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from atlas.core.settings import Settings
from atlas.domain.canonical import taxonomy
from atlas.domain.canonical.graph import TABLES, Dataset
from atlas.persistence.canonical import history, integrity, repository
from atlas.persistence.canonical import publish as publish_module
from atlas.persistence.canonical.publish import (
    AssertionInput,
    IntegrityCheckError,
    PublishRejected,
    PublishResult,
    publish,
)
from atlas.persistence.canonical.repository import OrgScope
from tests.integration.conftest import connect, connect_kwargs, tx_connect
from tests.integration.test_canonical_publish import LOADER, draft_from_graph
from tests.unit import canonical_graph as g

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "build" / "specs" / "acceptance" / "test-data" / "canonical-golden-v1"
ORGS = {
    "cp4-golden": "0190f000-0000-7000-8000-00000000c401",
    "cp4-import": "0190f000-0000-7000-8000-00000000c402",
    "cp4-other": "0190f000-0000-7000-8000-00000000c403",
}


def golden() -> tuple[Dataset, dict[str, taxonomy.Taxonomy], dict[str, Any]]:
    if not FIXTURE.exists():
        pytest.fail("pinned golden fixture missing: run `make specs`")
    tables = {t: json.loads((FIXTURE / f"{t}.json").read_text(encoding="utf-8")) for t in TABLES}
    policy = json.loads((FIXTURE / "trust-policy.json").read_text(encoding="utf-8"))
    base = taxonomy.current()
    taxes = {base.version: base}
    for path in sorted(FIXTURE.glob("taxonomy-overlay.*.json")):
        overlay = base.with_overlay(json.loads(path.read_text(encoding="utf-8")))
        taxes[overlay.version] = overlay
    return Dataset(tables, policy), taxes, policy


@pytest.fixture(scope="module")
def cp4_orgs(migrated: None, settings: Settings) -> Iterator[None]:
    overlay = json.loads(next(FIXTURE.glob("taxonomy-overlay.*.json")).read_text(encoding="utf-8"))
    with connect(settings.migration_database_url) as owner:
        owner.execute(
            "INSERT INTO atlas.taxonomy_release (version, status) VALUES (%s, %s)",
            (overlay["taxonomy_version"], overlay["status"]),
        )
        for provider, items in overlay["add_native_type_mappings"].items():
            for m in items:
                owner.execute(
                    "INSERT INTO atlas.native_type_mapping VALUES (%s, %s, %s, %s)",
                    (overlay["taxonomy_version"], provider, m["native_type"], m["category"]),
                )
        for key, oid in ORGS.items():
            owner.execute("INSERT INTO atlas.organization (id, key, display_name) VALUES (%s, %s, %s)", (oid, key, key))
            owner.execute("INSERT INTO atlas.org_revision (org_id, last) VALUES (%s, 0)", (oid,))
    try:
        yield
    finally:
        with connect(settings.migration_database_url) as owner:
            for oid in ORGS.values():
                owner.execute("DELETE FROM atlas.org_revision WHERE org_id = %s", (oid,))
                owner.execute("DELETE FROM atlas.organization WHERE id = %s", (oid,))
            owner.execute(
                "DELETE FROM atlas.native_type_mapping WHERE taxonomy_version = %s", (overlay["taxonomy_version"],)
            )
            owner.execute("DELETE FROM atlas.taxonomy_release WHERE version = %s", (overlay["taxonomy_version"],))


@pytest.fixture
def ingest_tx(cp4_orgs: None, ingest_url: str) -> Iterator[psycopg.Connection[Any]]:
    with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
        yield conn


def _same_rows(db: list[dict[str, Any]], fx: list[dict[str, Any]], org: str) -> bool:
    def norm(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps({**r, "org_id": None}, sort_keys=True, default=str) for r in rows)

    return norm(db) == norm([r for r in fx if r["org_id"] == org])


# --------------------------------------------------------------------------- AC-CP4-01/02/04: replay and reconstruction


def test_golden_history_replays_revision_by_revision(ingest_tx: psycopg.Connection[Any], settings: Settings) -> None:
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    expected = {
        e["revision"]: e for e in json.loads((FIXTURE / "expected-as-of.json").read_text()) if e["org_key"] == "default"
    }
    scope = repository.resolve_scope(conn, "cp4-golden")

    def check(revision: int, _result: PublishResult) -> None:
        e = expected[revision]
        assert history.hash_at(conn, scope, revision) == (e["entities"], e["relationships"], e["graph_hash"]), revision
        assert integrity.revision_findings(conn, scope, revision) == []

    results = history.replay(conn, scope, ds, source, taxes, policy, after_each=check)
    assert [r.revision for r in results] == list(range(1, 9))
    assert integrity.history_findings(conn, scope) == []
    for revision in range(1, 9):  # immutable history: every earlier revision still reconstructs identically
        e = expected[revision]
        assert history.hash_at(conn, scope, revision) == (e["entities"], e["relationships"], e["graph_hash"])
    loaded = repository.load(conn, scope, history=True)
    for name in (
        "entities",
        "entity_versions",
        "relationships",
        "relationship_versions",
        "external_identities",
        "evidence",
        "fact_assertions",
    ):
        assert _same_rows(loaded.tables[name], ds.tables[name], source), name
    # attribution: the configured loader identity, the server's role and time, the revert linked to r4
    css = sorted(loaded.tables["change_sets"], key=lambda c: c["revision"])
    assert {c["actor"]["namespace"] + ":" + c["actor"]["key"] for c in css} == {"atlas.service:fixture-loader"}
    assert {c["db_role"] for c in css} == {settings.ingestion_role}
    assert [(c["kind"], c["reverts_revision"]) for c in css][3:5] == [("fixture_load", None), ("revert", 4)]
    # snapshots: the fixture's labels reproduce the fixture's hashes; a label never moves
    for snap in (s for s in ds.tables["graph_snapshots"] if s["org_id"] == source):
        assert history.create_snapshot(conn, scope, snap["label"], snap["revision"])["graph_hash"] == snap["graph_hash"]
    assert history.verify_snapshots(conn, scope) == []
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        history.create_snapshot(conn, scope, "head", 1)
    # idempotency: the same inputs again write nothing
    last = history.recorded_drafts(ds, source)[-1]
    again = publish(
        conn,
        scope,
        LOADER,
        last.draft,
        taxes,
        policy,
        taxonomy_version=last.taxonomy_version,
        evaluated_at=last.change_set["created_at"],
    )
    assert again.revision is None
    batch = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.json").read_text())
    assert history.apply_import(conn, scope, batch, taxes, policy).result.revision is None
    assert repository.current_revision(conn, scope) == 8


# --------------------------------------------------------------------------- AC-CP4-03: reverts


def test_reverts_are_new_attributed_revisions_and_unsafe_ones_are_refused(ingest_tx: psycopg.Connection[Any]) -> None:
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    expected = {
        e["revision"]: e for e in json.loads((FIXTURE / "expected-as-of.json").read_text()) if e["org_key"] == "default"
    }
    scope = repository.resolve_scope(conn, "cp4-golden")
    history.replay(conn, scope, ds, source, taxes, policy)
    with pytest.raises(PublishRejected) as caught:  # r4's relationship was changed again (by the r5 revert)
        history.revert(conn, scope, g_steward(), 4, taxes, policy)
    assert caught.value.rule_ids == {"D-14"}
    assert repository.current_revision(conn, scope) == 8  # nothing written
    result = history.revert(conn, scope, g_steward(), 8, taxes, policy, reason="undo the r8 import")
    assert result.revision == 9
    e7 = expected[7]
    assert history.hash_at(conn, scope, 9) == (e7["entities"], e7["relationships"], e7["graph_hash"])  # back to r7
    e8 = expected[8]
    assert history.hash_at(conn, scope, 8) == (e8["entities"], e8["relationships"], e8["graph_hash"])  # history kept
    cs = next(c for c in repository.load(conn, scope).tables["change_sets"] if c["revision"] == 9)
    assert (cs["kind"], cs["reverts_revision"], cs["actor"]["namespace"]) == ("revert", 8, "oidc")
    with pytest.raises(PublishRejected):
        history.revert(conn, scope, g_steward(), 42, taxes, policy)


def g_steward() -> publish_module.Actor:
    return publish_module.Actor("human", "oidc", g.STEWARD["key"])


def _footprint(conn: psycopg.Connection[Any], scope: OrgScope) -> tuple[int, dict[str, str]]:
    """The organization's revision and every stored row: equal before and after means nothing was written."""
    ds = repository.load(conn, scope, history=True)
    rows = {
        name: json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in ds.tables[name])) for name in TABLES
    }
    return repository.current_revision(conn, scope), rows


def _entity_versions(conn: psycopg.Connection[Any], scope: OrgScope, display_name: str) -> list[dict[str, Any]]:
    ds = repository.load(conn, scope, history=True)
    found = [v for v in ds.tables["entity_versions"] if v["display_name"] == display_name]
    return sorted(found, key=lambda v: v["revision_from"])


def _reverted_ok(
    conn: psycopg.Connection[Any], scope: OrgScope, target: int, revision: int, expected: dict[int, Any]
) -> dict[str, Any]:
    """The revert is a new revision equal to the state before ``target``; ``target`` itself still reconstructs."""
    before, original = expected[target - 1], expected[target]
    assert history.hash_at(conn, scope, revision) == (before["entities"], before["relationships"], before["graph_hash"])
    assert history.hash_at(conn, scope, target) == (
        original["entities"],
        original["relationships"],
        original["graph_hash"],
    )
    assert integrity.revision_findings(conn, scope, revision) == []
    assert integrity.history_findings(conn, scope) == []
    return next(c for c in repository.load(conn, scope).tables["change_sets"] if c["revision"] == revision)


def test_reverting_a_steward_reclassification_restores_type_parent_and_identities(
    ingest_tx: psycopg.Connection[Any], settings: Settings
) -> None:
    """R6: a steward re-types a neutral data store to azure.sql_database, re-parents it and swaps its identities."""
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    expected = {
        e["revision"]: e for e in json.loads((FIXTURE / "expected-as-of.json").read_text()) if e["org_key"] == "default"
    }
    scope = repository.resolve_scope(conn, "cp4-golden")
    history.replay(conn, scope, ds, source, taxes, policy, until=6)
    result = history.revert(conn, scope, g_steward(), 6, taxes, policy, reason="undo the r6 reclassification")
    assert result.revision == 7
    cs = _reverted_ok(conn, scope, 6, 7, expected)
    assert (cs["kind"], cs["reverts_revision"], cs["channel"], cs["reason"]) == (
        "revert",
        6,
        "api",
        "undo the r6 reclassification",
    )
    assert (cs["actor"]["kind"], cs["actor"]["namespace"], cs["db_role"]) == ("human", "oidc", settings.ingestion_role)
    v5, v6, v7 = _entity_versions(conn, scope, "sqldb-billing-prod")[-3:]
    assert (v6["type_key"], v6["revision_from"], v6["revision_to"]) == ("azure.sql_database", 6, 7)  # history kept
    assert (
        (v7["type_key"], v7["taxonomy_version"])
        == (v5["type_key"], v5["taxonomy_version"])
        == (
            "infra.data_store",
            "1.1.0",
        )
    )
    assert {k: x for k, x in v7.items() if k not in ("revision_from", "revision_to")} == {
        k: x for k, x in v5.items() if k not in ("revision_from", "revision_to")
    }
    current = repository.load(conn, scope).tables["external_identities"]
    bound = {i["namespace"] for i in current if i["entity_id"] == v7["entity_id"]}
    recorded = {
        i["namespace"]
        for i in ds.tables["external_identities"]
        if i["entity_id"] == v7["entity_id"] and i["revision_from"] <= 5 and (i["revision_to"] or 99) > 5
    }
    assert bound == recorded  # the r5 identity bindings are back; the entity ID never changed


def test_reverting_a_taxonomy_migration_restores_the_historical_taxonomy_version(
    ingest_tx: psycopg.Connection[Any],
) -> None:
    """R7: a migration re-types infra.resource -> infra.data_store under taxonomy 1.2.0-test.1."""
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    expected = {
        e["revision"]: e for e in json.loads((FIXTURE / "expected-as-of.json").read_text()) if e["org_key"] == "default"
    }
    scope = repository.resolve_scope(conn, "cp4-golden")
    history.replay(conn, scope, ds, source, taxes, policy, until=7)
    result = history.revert(conn, scope, g_steward(), 7, taxes, policy, reason="undo the r7 migration")
    assert result.revision == 8
    cs = _reverted_ok(conn, scope, 7, 8, expected)
    assert (cs["kind"], cs["reverts_revision"]) == ("revert", 7)
    v6, v7, v8 = _entity_versions(conn, scope, "analytics-warehouse")[-3:]
    assert (v7["type_key"], v7["taxonomy_version"], v7["revision_to"]) == ("infra.data_store", "1.2.0-test.1", 8)
    assert (
        (v8["type_key"], v8["taxonomy_version"])
        == (v6["type_key"], v6["taxonomy_version"])
        == (
            "infra.resource",
            "1.1.0",
        )
    )


def test_a_reverted_version_keeps_the_taxonomy_version_it_was_validated_against(
    ingest_tx: psycopg.Connection[Any],
) -> None:
    """Undoing a later edit of the re-typed entity restores its 1.2.0-test.1 version, not a re-stamped copy."""
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    scope = repository.resolve_scope(conn, "cp4-golden")
    history.replay(conn, scope, ds, source, taxes, policy)
    before = _entity_versions(conn, scope, "analytics-warehouse")[-1]
    assert (before["type_key"], before["taxonomy_version"]) == ("infra.data_store", "1.2.0-test.1")
    edit = publish_module.ChangeSetDraft("steward_edit", "api", "rename", "steward rename")
    edit.entities.append(history.entity_input({**before, "display_name": "analytics-warehouse-eu"}))
    assert publish(conn, scope, g_steward(), edit, taxes, policy).revision == 9
    result = history.revert(conn, scope, g_steward(), 9, taxes, policy)
    assert result.revision == 10
    restored = _entity_versions(conn, scope, "analytics-warehouse")[-1]
    assert restored["revision_from"] == 10
    assert {k: x for k, x in restored.items() if k not in ("revision_from", "revision_to")} == {
        k: x for k, x in before.items() if k not in ("revision_from", "revision_to")
    }
    assert history.hash_at(conn, scope, 10) == history.hash_at(conn, scope, 8)
    assert integrity.history_findings(conn, scope) == []


def test_conflicting_reclassification_reverts_are_refused_and_write_nothing(
    ingest_tx: psycopg.Connection[Any],
) -> None:
    """At head, r8 reads from the data store that r7 created by re-typing: undoing r7 would leave a relationship the
    restored type does not allow (ADR-0008 §5.4). A fact changed again after its revision is a D-14 conflict."""
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    scope = repository.resolve_scope(conn, "cp4-golden")
    history.replay(conn, scope, ds, source, taxes, policy)
    edit = publish_module.ChangeSetDraft("steward_edit", "api", "rename", "steward rename")
    current = _entity_versions(conn, scope, "sqldb-billing-prod")[-1]
    edit.entities.append(history.entity_input({**current, "display_name": "sqldb-billing-prod-01"}))
    assert publish(conn, scope, g_steward(), edit, taxes, policy).revision == 9
    footprint = _footprint(conn, scope)
    with pytest.raises(PublishRejected) as dependent:
        history.revert(conn, scope, g_steward(), 7, taxes, policy)
    assert dependent.value.rule_ids == {"D-04"}
    assert _footprint(conn, scope) == footprint
    with pytest.raises(PublishRejected) as changed_again:  # r6's entity was renamed at r9
        history.revert(conn, scope, g_steward(), 6, taxes, policy)
    assert changed_again.value.rule_ids == {"D-14"}
    assert "sqldb-billing-prod" in str(changed_again.value) or current["entity_id"] in str(changed_again.value)
    assert _footprint(conn, scope) == footprint


# --------------------------------------------------------------------------- import through the planner (AC-CP4-01/02)


def test_import_batch_creates_exactly_revision_2_and_replays_as_a_no_op(ingest_tx: psycopg.Connection[Any]) -> None:
    conn = ingest_tx
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    scope = repository.resolve_scope(conn, "cp4-import")
    history.replay(conn, scope, ds, source, taxes, policy, until=1)
    batch = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.json").read_text())
    report = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.expected-report.json").read_text())
    outcome = history.apply_import(conn, scope, batch, taxes, policy)
    assert outcome.result.revision == 2
    assert outcome.plan.report() == report["rejected"]  # rejected and reported, never stored
    created = {e["id"] for e in repository.load(conn, scope).tables["entities"] if e["created_revision"] == 2}
    assert created == {e["id"] for e in ds.tables["entities"] if e["org_id"] == source and e["created_revision"] == 2}
    assert integrity.revision_findings(conn, scope, 2) == []
    cs = next(c for c in repository.load(conn, scope).tables["change_sets"] if c["revision"] == 2)
    assert (cs["kind"], cs["channel"], cs["actor"]["key"]) == ("import", "import", "import-worker")
    assert history.apply_import(conn, scope, batch, taxes, policy).result.revision is None
    assert repository.current_revision(conn, scope) == 2


# --------------------------------------------------------------------------- AC-CP4-06: trust recomputation


AGEING = {"freshness": [{"method": "import:document:v1", "stale_after_days": 30}], "precedence": []}  # test-only


def test_trust_is_recomputed_when_evidence_ages_or_changes(ingest_tx: psycopg.Connection[Any]) -> None:
    conn = ingest_tx
    scope = repository.resolve_scope(conn, "cp4-other")
    taxes = g.TAXES
    first = publish(conn, scope, LOADER, draft_from_graph(), taxes, AGEING, evaluated_at="2026-09-25T00:00:00Z")
    assert first.revision == 1
    assert history.recompute_trust(conn, scope, taxes, AGEING, evaluated_at="2026-09-26T00:00:00Z").revision is None
    aged = history.recompute_trust(conn, scope, taxes, AGEING, evaluated_at="2026-10-20T00:00:00Z")
    assert aged.revision == 2
    assert (aged.written.get("entity_version"), aged.written.get("relationship_version")) == (
        3,
        1,
    )  # document-only facts
    states = {v["entity_id"]: v["trust_state"] for v in repository.load(conn, scope).tables["entity_versions"]}
    assert {states[g.ENV], states[g.TEAM], states[g.APP]} == {"stale"}
    assert states[g.WEB] == "discovered"  # provider configuration has no freshness policy: unassessed, unchanged
    cs = next(c for c in repository.load(conn, scope).tables["change_sets"] if c["revision"] == 2)
    assert (cs["kind"], cs["channel"], cs["actor"]["kind"], cs["actor"]["key"]) == (
        "migration",
        "migration",
        "system",
        "trust-recompute",
    )
    assert history.recompute_trust(conn, scope, taxes, AGEING, evaluated_at="2026-10-20T00:00:00Z").revision is None
    # new evidence: a steward's assertion makes the team curated again (re-evaluated in the same change set)
    renewed = draft_from_graph(kind="steward_edit", channel="api")
    for e in renewed.entities:
        e.expected_trust_state = None  # the states have moved on since the original load
    for r in renewed.relationships:
        r.expected_trust_state = None
    renewed.assertions.append(AssertionInput(g.uid(900), "entity", g.TEAM, g.EV_HUMAN))
    result = publish(conn, scope, g_steward(), renewed, taxes, AGEING, evaluated_at="2026-10-21T00:00:00Z")
    assert result.revision == 3
    assert repository.entity_version(conn, scope, g.TEAM)["trust_state"] == "curated"  # type: ignore[index]


def test_an_expired_verification_turns_the_fact_stale(ingest_tx: psycopg.Connection[Any]) -> None:
    conn = ingest_tx
    scope = repository.resolve_scope(conn, "cp4-other")
    draft = draft_from_graph()
    web = next(e for e in draft.entities if e.entity_id == g.WEB)
    assert web.verification is not None
    web.verification = {**web.verification, "expires_at": "2026-10-10T00:00:00Z"}
    publish(conn, scope, LOADER, draft, g.TAXES, evaluated_at="2026-09-25T00:00:00Z")
    result = history.recompute_trust(conn, scope, g.TAXES, evaluated_at="2026-10-11T00:00:00Z")
    assert result.written == {"entity_version": 1}
    assert repository.entity_version(conn, scope, g.WEB)["trust_state"] == "stale"  # type: ignore[index]


# --------------------------------------------------------------------------- concurrency, failure and recovery


def test_concurrent_publishers_are_serialized_and_revisions_stay_gap_free(cp4_orgs: None, ingest_url: str) -> None:
    holder = psycopg.connect(**connect_kwargs(ingest_url))
    done: dict[str, Any] = {}
    try:
        scope = repository.resolve_scope(holder, "cp4-other")
        holder.execute("SELECT last FROM atlas.org_revision WHERE org_id = %s FOR UPDATE", (scope.org_id,))

        def second() -> None:
            with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
                started = time.monotonic()
                done["result"] = publish(conn, scope, LOADER, draft_from_graph(), g.TAXES)
                done["waited"] = time.monotonic() - started

        worker = threading.Thread(target=second)
        worker.start()
        time.sleep(1.0)
        assert worker.is_alive()  # blocked on the organization's revision counter
        holder.rollback()  # the first writer aborts: its revision number is not consumed
        worker.join(timeout=60)
        assert not worker.is_alive()
        assert done["result"].revision == 1
        assert done["waited"] >= 0.9
    finally:
        holder.close()


def test_a_failure_after_writing_rolls_everything_back(
    ingest_tx: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = ingest_tx
    scope = repository.resolve_scope(conn, "cp4-other")
    monkeypatch.setattr(integrity, "revision_findings", lambda *_: ["H-10 simulated failure"])
    with pytest.raises(IntegrityCheckError):
        publish(conn, scope, LOADER, draft_from_graph(), g.TAXES)
    monkeypatch.undo()
    assert repository.current_revision(conn, scope) == 0
    assert repository.load(conn, scope).tables["entities"] == []  # nothing of the failed change set survives
    assert publish(conn, scope, LOADER, draft_from_graph(), g.TAXES).revision == 1  # the number is reused: gap-free


def test_composite_keys_keep_organizations_apart(cp4_orgs: None, settings: Settings) -> None:
    with tx_connect(settings.migration_database_url) as conn, conn.transaction(force_rollback=True):
        a, b = OrgScope(ORGS["cp4-golden"], "cp4-golden"), OrgScope(ORGS["cp4-other"], "cp4-other")
        publish(conn, a, LOADER, draft_from_graph(), g.TAXES)
        conn.execute(
            "INSERT INTO atlas.change_set (org_id, revision, kind, channel, actor_kind, actor_namespace, actor_key, "
            "source_ref, reason) VALUES (%s, 1, 'fixture_load', 'fixture_cli', 'service', 'atlas.service', "
            "'fixture-loader', 'test', 'test')",
            (b.org_id,),
        )
        conn.execute(
            "INSERT INTO atlas.entity (org_id, id, created_revision) VALUES (%s, %s, 1)", (b.org_id, g.uid(950))
        )
        with pytest.raises(errors.ForeignKeyViolation), conn.transaction():  # a container in another organization
            conn.execute(
                "INSERT INTO atlas.entity_version (org_id, entity_id, revision_from, type_key, category, record_state, "
                "display_name, lifecycle, attributes, trust_state, verification_state, taxonomy_version, container_id) "
                "VALUES (%s, %s, 1, 'core.service', 'core.service', 'present', 'x', 'active', '{}', 'documented', "
                "'unverified', '1.1.0', %s)",
                (b.org_id, g.uid(950), g.APP),
            )
        assert repository.entity_version(conn, b, g.APP) is None  # and the repository answers "not found"
