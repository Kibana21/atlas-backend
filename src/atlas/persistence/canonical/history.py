"""Revision history operations on the canonical store (ATLAS-002 CP4; ADR-0008 §4, §7, §8).

Everything writes through :func:`atlas.persistence.canonical.publish.publish`, so every operation is an ordinary,
attributed, validated change set:

* :func:`revert`: a **new** change set restoring every fact changed by a revision to its state before it; refused
  with the conflicting facts listed when a later revision changed one of them (ADR-0008 §7);
* :func:`recompute_trust`: a recompute change set re-evaluating every current fact (trust-function.md);
* :func:`apply_import`: an import batch through the import planner, with the import report;
* :func:`replay`: recorded history (a golden fixture) replayed revision by revision, as the fixture loader;
* :func:`create_snapshot` / :func:`verify_snapshots`: named, immutable pointers with graph hashes (ADR-0008 §4).

Actors always come from the caller's configuration (the entry point), never from the replayed or imported data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from atlas.core.ids import uuid7
from atlas.domain.canonical import rules
from atlas.domain.canonical.graph import Dataset, Row, as_of, graph_hash, sha256_of
from atlas.domain.canonical.importer import Plan, plan_batch
from atlas.domain.canonical.taxonomy import Taxonomy
from atlas.persistence.canonical import repository
from atlas.persistence.canonical.publish import (
    Actor,
    AssertionInput,
    ChangeSetDraft,
    EntityInput,
    EvidenceInput,
    IdentityInput,
    PublishRejected,
    PublishResult,
    RelationshipInput,
    publish,
)
from atlas.persistence.canonical.repository import OrgScope

Connection = psycopg.Connection[Any]
IdFactory = Callable[[str], str]  # (purpose) -> new UUIDv7 string
VERIFICATION_FIELDS = ("verification_state", "verified_by", "verified_at", "expires_at")
FIXTURE_LOADER = Actor("service", "atlas.service", "fixture-loader", "Fixture loader")
IMPORT_WORKER = Actor("service", "atlas.service", "import-worker", "Import worker")
TRUST_RECOMPUTE = Actor("system", "atlas.system", "trust-recompute", "Trust recomputation")


def new_id(_purpose: str) -> str:
    return str(uuid7())


# --------------------------------------------------------------------------- row -> input


def entity_input(v: Row, *, expected_trust: bool = False) -> EntityInput:
    return EntityInput(
        v["entity_id"],
        v["type_key"],
        v["display_name"],
        v["lifecycle"],
        v["record_state"],
        v["merged_into"],
        v["provider"],
        v["native_type"],
        v["environment_scope"],
        v["environment_id"],
        v["container_id"],
        dict(v["attributes"]),
        {k: v[k] for k in VERIFICATION_FIELDS},
        expected_trust_state=v["trust_state"] if expected_trust and v["record_state"] == "present" else None,
    )


def relationship_input(r: Row, v: Row, *, expected_trust: bool = False) -> RelationshipInput:
    return RelationshipInput(
        r["id"],
        r["rel_type"],
        r["from_id"],
        r["to_id"],
        dict(v["attributes"]),
        v["lifecycle"],
        v["record_state"],
        {k: v[k] for k in VERIFICATION_FIELDS},
        expected_trust_state=v["trust_state"] if expected_trust and v["record_state"] == "present" else None,
    )


def assertion_input(a: Row, assertion_id: str | None = None) -> AssertionInput:
    return AssertionInput(
        assertion_id or a["id"], a["fact_kind"], a["fact_id"], a["evidence_id"], a["field_path"], a["asserted_value"]
    )


def evidence_input(e: Row) -> EvidenceInput:
    return EvidenceInput(
        e["id"],
        e["kind"],
        e["source_ref"],
        e["method"],
        e["observed_at"],
        e["artifact_ref"],
        e["artifact_hash"],
        e["confidence"],
        e["valid_from"],
        e["valid_to"],
    )


# --------------------------------------------------------------------------- revert (ADR-0008 §7)


def revert_draft(ds: Dataset, org_id: str, target: int, head: int, ids: IdFactory, reason: str) -> ChangeSetDraft:
    """The change set that undoes ``target``; raises :class:`PublishRejected` (D-14) on a revert conflict."""
    if not 1 <= target <= head:
        raise PublishRejected([rules.Finding("D-14", rules.DOMAIN, f"revision {target} does not exist (head {head})")])
    t = ds.tables
    rels = {r["id"]: r for r in t["relationships"]}
    draft = ChangeSetDraft("revert", "api", f"revert of r{target}", reason, reverts_revision=target)
    conflicts: list[str] = []
    for table, key in (("entity_versions", "entity_id"), ("relationship_versions", "relationship_id")):
        mine = [v for v in t[table] if v["org_id"] == org_id]
        for fid in sorted({v[key] for v in mine if v["revision_from"] == target}):
            versions = [v for v in mine if v[key] == fid]
            if any(v["revision_from"] > target for v in versions):
                conflicts.append(f"{table[:-9]} {fid} changed again after r{target}")
                continue
            prior = next((v for v in versions if v["revision_to"] == target), None)
            current = next(v for v in versions if v["revision_to"] is None)
            restored = {**(prior or current), "record_state": prior["record_state"] if prior else "removed"}
            if key == "entity_id":
                draft.entities.append(entity_input(restored))
            else:
                draft.relationships.append(relationship_input(rels[fid], restored))
    for a in (a for a in t["fact_assertions"] if a["org_id"] == org_id):
        if a["revision_from"] == target:
            if a["revision_to"] is None:
                draft.close_assertions.append(a["id"])
            else:
                conflicts.append(f"assertion {a['id']} closed again after r{target}")
        elif a["revision_to"] == target:
            draft.assertions.append(assertion_input(a, ids(f"assertion:{a['id']}")))
    for i in (i for i in t["external_identities"] if i["org_id"] == org_id):
        if i["revision_from"] == target and i["revision_to"] is None:
            draft.close_identities.append(i["id"])
        elif i["revision_to"] == target:
            draft.identities.append(
                IdentityInput(
                    ids(f"identity:{i['id']}"), i["entity_id"], i["namespace"], i["original_key"], i["match_method"]
                )
            )
    if conflicts:
        raise PublishRejected(
            [rules.Finding("D-14", rules.DOMAIN, f"revert of r{target} refused: {c}") for c in conflicts]
        )
    return draft


def revert(
    conn: Connection,
    scope: OrgScope,
    actor: Actor,
    target: int,
    taxes: dict[str, Taxonomy],
    policy: dict[str, Any] | None = None,
    *,
    ids: IdFactory = new_id,
    reason: str | None = None,
    channel: str = "api",
    evaluated_at: str | None = None,
) -> PublishResult:
    """Revert revision ``target`` as a new change set (kind ``revert``), or raise :class:`PublishRejected`."""
    with conn.transaction():
        conn.execute("SELECT last FROM atlas.org_revision WHERE org_id = %s FOR UPDATE", (scope.org_id,))
        ds = repository.load(conn, scope, history=True)
        draft = revert_draft(ds, scope.org_id, target, ds.head(scope.org_id), ids, reason or f"revert of r{target}")
        draft.channel = channel
        return publish(conn, scope, actor, draft, taxes, policy, evaluated_at=evaluated_at)


# --------------------------------------------------------------------------- trust recomputation


def recompute_trust(
    conn: Connection,
    scope: OrgScope,
    taxes: dict[str, Taxonomy],
    policy: dict[str, Any] | None = None,
    *,
    actor: Actor = TRUST_RECOMPUTE,
    evaluated_at: str | None = None,
) -> PublishResult:
    """Re-evaluate every current fact with trust function v1; a new version only where the result changed.

    Written as a ``migration``-kind change set on the ``migration`` channel by a system actor (ADR-0018: a function
    change, or time-driven expiry and ageing, is applied in one change set). No change: no change set.
    """
    draft = ChangeSetDraft("migration", "migration", "trust-1 recomputation", "trust function v1 recomputation")
    return publish(conn, scope, actor, draft, taxes, policy, recompute=True, evaluated_at=evaluated_at)


# --------------------------------------------------------------------------- import (A2-T17)


@dataclass
class ImportOutcome:
    plan: Plan
    result: PublishResult


def import_draft(
    batch: Row, plan: Plan, st_evidence: dict[str, str], ids: IdFactory, current: Dataset
) -> ChangeSetDraft:
    """Change set for a plan. Evidence already stored (same artifact hash) is reused, so a replay adds nothing."""
    draft = ChangeSetDraft(
        "import",
        batch.get("channel", "import"),
        batch["source_ref"],
        batch["reason"],
        input_hash=sha256_of(batch),
        change_reference=batch.get("change_reference"),
    )
    ev_ids: dict[str, str] = {}
    for e in batch["evidence"]:
        existing = st_evidence.get(e["artifact_hash"]) if e.get("artifact_hash") else None
        ev_ids[e["ref"]] = existing or ids(f"evidence:{e['ref']}")
        if existing is None:
            draft.evidence.append(
                EvidenceInput(
                    ev_ids[e["ref"]],
                    e["kind"],
                    e["source_ref"],
                    e["method"],
                    e["observed_at"],
                    e.get("artifact_ref"),
                    e.get("artifact_hash"),
                    e.get("confidence", 1.0),
                )
            )
    for d in plan.new_entities:
        draft.entities.append(
            EntityInput(
                d["entity_id"],
                d["type_key"],
                d["display_name"],
                d["lifecycle"],
                "present",
                None,
                d["provider"],
                d["native_type"],
                d["environment_scope"],
                d["environment_id"],
                d["container_id"],
                d["attributes"],
            )
        )
        for i in d["identities"]:
            draft.identities.append(
                IdentityInput(
                    ids(f"identity:{d['entity_id']}:{i['namespace']}"),
                    d["entity_id"],
                    i["namespace"],
                    i["original_key"],
                    "provider_id",
                )
            )
        for ref in d["evidence_refs"]:
            draft.assertions.append(
                AssertionInput(ids(f"assertion:{d['entity_id']}"), "entity", d["entity_id"], ev_ids[ref])
            )
    versions = {v["entity_id"]: v for v in current.tables["entity_versions"] if v["revision_to"] is None}
    for eid, change in plan.changed_entities:  # new version with the changed fields, new identities and evidence
        draft.entities.append(entity_input({**versions[eid], **change["fields"]}))
        for i in change["identities"]:
            draft.identities.append(
                IdentityInput(
                    ids(f"identity:{eid}:{i['namespace']}"), eid, i["namespace"], i["original_key"], "provider_id"
                )
            )
        for ref in change["evidence_refs"]:
            draft.assertions.append(AssertionInput(ids(f"assertion:{eid}"), "entity", eid, ev_ids[ref]))
    rels = {r["id"]: r for r in current.tables["relationships"]}
    rversions = {v["relationship_id"]: v for v in current.tables["relationship_versions"] if v["revision_to"] is None}
    for rid, change in plan.changed_relationships:
        draft.relationships.append(
            relationship_input(rels[rid], {**rversions[rid], "attributes": change["attributes"]})
        )
        for ref in change["evidence_refs"]:
            draft.assertions.append(AssertionInput(ids(f"assertion:{rid}"), "relationship", rid, ev_ids[ref]))
    for parent, child in plan.new_containments:
        rid = ids(f"contains:{parent}>{child}")
        draft.relationships.append(RelationshipInput(rid, "CONTAINS", parent, child))
        child_refs = next(d["evidence_refs"] for d in plan.new_entities if d["entity_id"] == child)
        for ref in child_refs:
            draft.assertions.append(AssertionInput(ids(f"assertion:{rid}"), "relationship", rid, ev_ids[ref]))
    for r in plan.new_relationships:
        draft.relationships.append(RelationshipInput(r["id"], r["rel_type"], r["from_id"], r["to_id"], r["attributes"]))
        for ref in r["evidence_refs"]:
            draft.assertions.append(AssertionInput(ids(f"assertion:{r['id']}"), "relationship", r["id"], ev_ids[ref]))
    return draft


def apply_import(
    conn: Connection,
    scope: OrgScope,
    batch: Row,
    taxes: dict[str, Taxonomy],
    policy: dict[str, Any] | None = None,
    *,
    actor: Actor = IMPORT_WORKER,
    ids: IdFactory = new_id,
    evaluated_at: str | None = None,
) -> ImportOutcome:
    """Plan ``batch`` against the current graph and publish exactly the plan; rejected items go to the report."""
    with conn.transaction():
        conn.execute("SELECT last FROM atlas.org_revision WHERE org_id = %s FOR UPDATE", (scope.org_id,))
        ds = repository.load(conn, scope)
        st = as_of(ds, scope.org_id, ds.head(scope.org_id))
        hashes = {
            e["artifact_hash"]: e["id"]
            for e in repository.load(conn, scope, history=True).tables["evidence"]
            if e["artifact_hash"]
        }
        plan = plan_batch(taxes[batch["taxonomy_version"]], batch, st)
        if plan.empty:
            return ImportOutcome(plan, PublishResult(None))
        draft = import_draft(batch, plan, hashes, ids, ds)
        return ImportOutcome(plan, publish(conn, scope, actor, draft, taxes, policy, evaluated_at=evaluated_at))


# --------------------------------------------------------------------------- recorded-history replay (A2-T16)


@dataclass
class Recorded:
    revision: int
    change_set: Row
    draft: ChangeSetDraft
    taxonomy_version: str


def recorded_drafts(ds: Dataset, org_id: str) -> list[Recorded]:
    """One draft per recorded revision of ``org_id``: exactly the rows that revision wrote or closed, IDs included."""
    t = ds.tables
    rels = {r["id"]: r for r in t["relationships"]}
    out: list[Recorded] = []
    for cs in sorted((c for c in t["change_sets"] if c["org_id"] == org_id), key=lambda c: c["revision"]):
        r = cs["revision"]
        draft = ChangeSetDraft(
            "fixture_load",
            "fixture_cli",
            f"recorded r{r}: {cs['source_ref']}",
            cs["reason"],
            input_hash=None,
            change_reference=cs["change_reference"],
        )

        def mine(table: str) -> list[Row]:
            return [x for x in t[table] if x["org_id"] == org_id]

        draft.evidence = [evidence_input(e) for e in mine("evidence") if e["change_revision"] == r]
        versions = [v for v in mine("entity_versions") if v["revision_from"] == r]
        draft.entities = [entity_input(v, expected_trust=True) for v in versions]
        rversions = [v for v in mine("relationship_versions") if v["revision_from"] == r]
        draft.relationships = [
            relationship_input(rels[v["relationship_id"]], v, expected_trust=True) for v in rversions
        ]
        draft.identities = [
            IdentityInput(i["id"], i["entity_id"], i["namespace"], i["original_key"], i["match_method"])
            for i in mine("external_identities")
            if i["revision_from"] == r
        ]
        draft.close_identities = [i["id"] for i in mine("external_identities") if i["revision_to"] == r]
        draft.assertions = [assertion_input(a) for a in mine("fact_assertions") if a["revision_from"] == r]
        draft.close_assertions = [a["id"] for a in mine("fact_assertions") if a["revision_to"] == r]
        tax_versions = {v["taxonomy_version"] for v in versions + rversions} or {_previous_taxonomy(out)}
        draft.input_hash = sha256_of({"revision": r, "versions": versions, "relationship_versions": rversions})
        out.append(Recorded(r, cs, draft, sorted(tax_versions)[-1]))
    return out


def _previous_taxonomy(done: list[Recorded]) -> str:
    return done[-1].taxonomy_version if done else "1.1.0"


def replay(
    conn: Connection,
    scope: OrgScope,
    ds: Dataset,
    org_id: str,
    taxes: dict[str, Taxonomy],
    policy: dict[str, Any] | None = None,
    *,
    actor: Actor = FIXTURE_LOADER,
    after_each: Callable[[int, PublishResult], None] | None = None,
    until: int | None = None,
) -> list[PublishResult]:
    """Replay ``org_id``'s recorded history into ``scope`` (which must be empty), revision by revision.

    Each revision is evaluated at its recorded change-set time, so the stored trust states are reproduced exactly.
    Recorded reverts are replayed as reverts (with the recorded IDs of the restored assertions and identities).
    """
    if repository.current_revision(conn, scope) != 0:
        raise PublishRejected([rules.Finding("H-05", rules.HARD, "replay needs an organization with no revision yet")])
    results = []
    for rec in recorded_drafts(ds, org_id):
        if until is not None and rec.revision > until:
            break
        at = rec.change_set["created_at"]
        if rec.change_set["kind"] == "revert":
            restored = {
                (a["fact_id"], a["field_path"], a["evidence_id"]): a["id"]
                for a in ds.tables["fact_assertions"]
                if a["org_id"] == org_id and a["revision_from"] == rec.revision
            }
            originals = {a["id"]: a for a in ds.tables["fact_assertions"] if a["org_id"] == org_id}

            def recorded_id(
                purpose: str, restored: dict[tuple[Any, ...], str] = restored, originals: dict[str, Row] = originals
            ) -> str:
                kind, _, old = purpose.partition(":")
                a = originals.get(old) if kind == "assertion" else None
                return restored[(a["fact_id"], a["field_path"], a["evidence_id"])] if a else new_id(purpose)

            result = revert(
                conn,
                scope,
                actor,
                rec.change_set["reverts_revision"],
                taxes,
                policy,
                ids=recorded_id,
                channel="fixture_cli",
                evaluated_at=at,
                reason=rec.change_set["reason"],
            )
        else:
            result = publish(
                conn, scope, actor, rec.draft, taxes, policy, taxonomy_version=rec.taxonomy_version, evaluated_at=at
            )
        if result.revision != rec.revision:
            raise PublishRejected(
                [rules.Finding("H-05", rules.HARD, f"recorded r{rec.revision} produced {result.revision}")]
            )
        results.append(result)
        if after_each:
            after_each(rec.revision, result)
    return results


# --------------------------------------------------------------------------- snapshots (ADR-0008 §4)


def hash_at(conn: Connection, scope: OrgScope, revision: int) -> tuple[int, int, str]:
    """(entities, relationships, graph hash) of the organization as of ``revision``, from the database."""
    st = as_of(repository.load(conn, scope, history=True), scope.org_id, revision)
    return len(st.entities), len(st.relationships), graph_hash(st)


def create_snapshot(
    conn: Connection, scope: OrgScope, label: str, revision: int | None = None, snapshot_id: str | None = None
) -> Row:
    """Insert an immutable snapshot (a label never moves: the label is unique per organization)."""
    rev = repository.current_revision(conn, scope) if revision is None else revision
    row = {
        "org_id": scope.org_id,
        "id": snapshot_id or str(uuid7()),
        "revision": rev,
        "label": label,
        "status": "published",
        "graph_hash": hash_at(conn, scope, rev)[2],
    }
    conn.execute(
        "INSERT INTO atlas.graph_snapshot (org_id, id, revision, label, status, graph_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (row["org_id"], row["id"], rev, label, row["status"], row["graph_hash"]),
    )
    return row


def verify_snapshots(conn: Connection, scope: OrgScope) -> list[str]:
    """Labels whose stored graph hash no longer equals the reconstructed graph at their revision (expected: none)."""
    ds = repository.load(conn, scope, history=True)
    return [
        s["label"]
        for s in ds.tables["graph_snapshots"]
        if graph_hash(as_of(ds, scope.org_id, s["revision"])) != s["graph_hash"]
    ]
