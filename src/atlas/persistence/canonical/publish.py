"""The publishing procedure: the only write path to the canonical store (ADR-0008 §3; A002-FR-08).

One change set, one transaction, run as the **ingestion role** (DEC-044 as amended):

1. lock the organization's revision counter (``FOR UPDATE``): writers are serialized, revisions are gap-free and
   committed in order; ``R = last + 1`` and the evaluation time is the transaction's server time;
2. load the organization's current state and compute the difference with the draft. **No difference: nothing is
   written and no revision is consumed** (idempotent);
3. maintain derived fields: category, discriminator, relationship flags (ADR-0017 §4.4) and the trust state
   (trust function v1). A fact whose trust or flags change gets a new version in the same change set;
4. validate revision R with the domain rules (HARD and DOMAIN; ADVISORY findings are returned, never blocking);
5. insert the change set (the server sets ``created_at`` and ``db_role``), close superseded versions
   (``revision_to = R``, set once) and insert the new rows;
6. run the in-database integrity checks for R, then advance the counter. Any failure rolls everything back.

The actor comes from the caller's execution context (the ingestion entry point), never from a payload (§10).
Reverts (ADR-0008 §7) are built by :func:`revert` and published here as ordinary, attributed change sets.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import psycopg

from atlas.domain.canonical import rules
from atlas.domain.canonical.graph import Dataset, Row, discriminator_of, relationship_flags, sha256_of, valid_at
from atlas.domain.canonical.identity import NormalizationError, normalize
from atlas.domain.canonical.taxonomy import Taxonomy
from atlas.domain.canonical.trust import NO_POLICY, Claim, evaluate, timestamp
from atlas.persistence.canonical import integrity, repository
from atlas.persistence.canonical.repository import OrgScope
from atlas.persistence.canonical.rows import FIXTURE_TABLE, format_ts, insert_sql, to_db

Connection = psycopg.Connection[Any]
REVISION_FIELDS = ("revision_from", "revision_to", "taxonomy_version")
TOMBSTONE_TRUST = (
    ("provider_configuration", "discovered"),
    ("human_assertion", "curated"),
    ("documentation", "documented"),
    ("contract", "documented"),
    ("inferred", "inferred"),
)
UNVERIFIED = {"verification_state": "unverified", "verified_by": None, "verified_at": None, "expires_at": None}


@dataclass(frozen=True)
class Actor:
    """Who initiated the change set: from the execution context only (ADR-0008 §10)."""

    kind: str
    namespace: str
    key: str
    label: str | None = None


@dataclass
class EntityInput:
    """The desired state of one entity at the new revision."""

    entity_id: str
    type_key: str
    display_name: str
    lifecycle: str = "active"
    record_state: str = "present"
    merged_into: str | None = None
    provider: str | None = None
    native_type: str | None = None
    environment_scope: str | None = None
    environment_id: str | None = None
    container_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] | None = None
    expected_trust_state: str | None = None
    taxonomy_version: str | None = (
        None  # a restored version keeps its recorded one (revert; D-14); default: the publisher's
    )


@dataclass
class RelationshipInput:
    relationship_id: str
    rel_type: str
    from_id: str
    to_id: str
    attributes: dict[str, Any] = field(default_factory=dict)
    lifecycle: str = "active"
    record_state: str = "present"
    verification: dict[str, Any] | None = None
    expected_trust_state: str | None = None
    taxonomy_version: str | None = (
        None  # a restored version keeps its recorded one (revert; D-14); default: the publisher's
    )


@dataclass
class IdentityInput:
    identity_id: str
    entity_id: str
    namespace: str
    original_key: str
    match_method: str


@dataclass
class EvidenceInput:
    evidence_id: str
    kind: str
    source_ref: str
    method: str
    observed_at: str
    artifact_ref: str | None = None
    artifact_hash: str | None = None
    confidence: float = 1.0
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass
class AssertionInput:
    assertion_id: str
    fact_kind: str
    fact_id: str
    evidence_id: str
    field_path: str | None = None
    asserted_value: Any = None


@dataclass
class ChangeSetDraft:
    kind: str
    channel: str
    source_ref: str
    reason: str
    input_hash: str | None = None
    change_reference: str | None = None
    reverts_revision: int | None = None  # kind "revert" only; build reverts with :func:`revert`
    evidence: list[EvidenceInput] = field(default_factory=list)
    entities: list[EntityInput] = field(default_factory=list)
    relationships: list[RelationshipInput] = field(default_factory=list)
    identities: list[IdentityInput] = field(default_factory=list)
    close_identities: list[str] = field(default_factory=list)
    assertions: list[AssertionInput] = field(default_factory=list)
    close_assertions: list[str] = field(default_factory=list)


@dataclass
class PublishResult:
    revision: int | None  # None: no difference, nothing written
    written: dict[str, int] = field(default_factory=dict)
    closed: dict[str, int] = field(default_factory=dict)
    advisories: list[rules.Finding] = field(default_factory=list)


class PublishRejected(Exception):  # noqa: N818 - domain wording
    """The change set violates a HARD or DOMAIN rule. Nothing was written."""

    def __init__(self, findings: list[rules.Finding]) -> None:
        self.findings = findings
        super().__init__(rules.describe(findings))

    @property
    def rule_ids(self) -> set[str]:
        return {f.rule for f in self.findings}


class IntegrityCheckError(Exception):
    """An in-database integrity check failed after writing; the transaction was rolled back."""


def _same(old: Row, content: Row, *, with_trust: bool) -> bool:
    ignored = REVISION_FIELDS if with_trust else (*REVISION_FIELDS, "trust_state")
    return all(old.get(k) == v for k, v in content.items() if k not in ignored)


class _Plan:
    """The difference between the current state and the draft, built on a copy of the current rows."""

    def __init__(self, scope: OrgScope, base: Dataset, tax: Taxonomy, revision: int, at: str) -> None:
        self.scope, self.tax, self.rev, self.at = scope, tax, revision, at
        self.ds = Dataset(copy.deepcopy(base.tables), base.policy)
        self.new: dict[str, list[Row]] = {}
        self.closed: dict[str, list[Row]] = {}
        self.findings: list[rules.Finding] = []
        t = self.ds.tables
        self.entity_ids = {e["id"] for e in t["entities"]}
        self.relationships = {r["id"]: r for r in t["relationships"]}
        self.current_ev = {v["entity_id"]: v for v in t["entity_versions"] if v["revision_to"] is None}
        self.current_rv = {v["relationship_id"]: v for v in t["relationship_versions"] if v["revision_to"] is None}
        self.evidence = {e["id"]: e for e in t["evidence"]}
        self.asserts_by_fact: dict[str, list[Row]] = {}
        for a in t["fact_assertions"]:
            self.asserts_by_fact.setdefault(a["fact_id"], []).append(a)

    # ---- helpers
    def reject(self, rule: str, message: str) -> None:
        self.findings.append(rules.Finding(rule, rules.HARD if rule.startswith("H-") else rules.DOMAIN, message))

    def add(self, fixture_table: str, row: Row) -> None:
        self.ds.tables[fixture_table].append(row)
        self.new.setdefault(fixture_table, []).append(row)
        if fixture_table == "fact_assertions":
            self.asserts_by_fact.setdefault(row["fact_id"], []).append(row)

    def close(self, fixture_table: str, row: Row) -> None:
        row["revision_to"] = self.rev
        self.closed.setdefault(fixture_table, []).append(row)

    @property
    def empty(self) -> bool:
        return not self.new and not self.closed

    # ---- draft items
    def evidence_rows(self, items: list[EvidenceInput]) -> None:
        for e in items:
            row = {
                "org_id": self.scope.org_id,
                "id": e.evidence_id,
                "kind": e.kind,
                "source_ref": e.source_ref,
                "artifact_ref": e.artifact_ref,
                "artifact_hash": e.artifact_hash,
                "method": e.method,
                "confidence": e.confidence,
                "observed_at": e.observed_at,
                "valid_from": e.valid_from,
                "valid_to": e.valid_to,
                "change_revision": self.rev,
            }
            existing = self.evidence.get(e.evidence_id)
            if existing is not None:
                if {k: v for k, v in existing.items() if k != "change_revision"} != {
                    k: v for k, v in row.items() if k != "change_revision"
                }:
                    self.reject("H-02", f"evidence {e.evidence_id} already exists with different content")
                continue
            self.evidence[e.evidence_id] = row
            self.add("evidence", row)

    def identity_rows(self, items: list[IdentityInput], close_ids: list[str]) -> None:
        current = {i["id"]: i for i in self.ds.tables["external_identities"] if i["revision_to"] is None}
        for identity_id in close_ids:
            if identity_id not in current:
                self.reject("H-04", f"identity {identity_id} is not a current binding in this organization")
                continue
            self.close("external_identities", current[identity_id])
        bound = {(i["namespace"], i["normalized_key"]): i for i in current.values() if i["revision_to"] is None}
        for item in items:
            try:
                key = normalize(self.tax, item.namespace, item.original_key)
            except NormalizationError as exc:
                self.reject("H-08", f"identity {item.identity_id}: {exc}")
                continue
            same = bound.get((item.namespace, key.normalized_key))
            if same is not None and same["entity_id"] == item.entity_id:
                continue  # already bound: no change
            self.add(
                "external_identities",
                {
                    "org_id": self.scope.org_id,
                    "id": item.identity_id,
                    "entity_id": item.entity_id,
                    "namespace": item.namespace,
                    "original_key": item.original_key,
                    "normalized_key": key.normalized_key,
                    "normalization_version": key.normalization_version,
                    "key_parts": key.key_parts,
                    "match_method": item.match_method,
                    "revision_from": self.rev,
                    "revision_to": None,
                },
            )

    def assertion_rows(self, items: list[AssertionInput], close_ids: list[str]) -> set[str]:
        """Returns the facts whose assertions changed (their trust is re-evaluated)."""
        touched: set[str] = set()
        current = {a["id"]: a for a in self.ds.tables["fact_assertions"] if a["revision_to"] is None}
        for assertion_id in close_ids:
            if assertion_id not in current:
                self.reject("H-04", f"assertion {assertion_id} is not current in this organization")
                continue
            self.close("fact_assertions", current[assertion_id])
            touched.add(current[assertion_id]["fact_id"])
        same = {
            (a["fact_id"], a["field_path"], sha256_of(a["asserted_value"]), a["evidence_id"])
            for a in current.values()
            if a["revision_to"] is None
        }
        for item in items:
            value = None if item.field_path is None else item.asserted_value
            if (item.fact_id, item.field_path, sha256_of(value), item.evidence_id) in same:
                continue
            ev = self.evidence.get(item.evidence_id)
            if ev is None:
                self.reject("H-04", f"assertion {item.assertion_id}: evidence {item.evidence_id} does not resolve")
                continue
            self.add(
                "fact_assertions",
                {
                    "org_id": self.scope.org_id,
                    "id": item.assertion_id,
                    "fact_kind": item.fact_kind,
                    "fact_id": item.fact_id,
                    "field_path": item.field_path,
                    "asserted_value": value,
                    "evidence_id": item.evidence_id,
                    "evidence_kind": ev["kind"],
                    "revision_from": self.rev,
                    "revision_to": None,
                },
            )
            touched.add(item.fact_id)
        return touched

    def _version(
        self,
        fixture_table: str,
        key: str,
        fact_id: str,
        content: Row,
        current: dict[str, Row],
        *,
        with_trust: bool = False,
        taxonomy_version: str | None = None,
    ) -> bool:
        """Close the current version and add ``content`` at R, unless nothing changed. True if a version was added."""
        old = current.get(fact_id)
        if old is not None and _same(old, content, with_trust=with_trust):
            return False
        if old is not None:
            if old["revision_from"] == self.rev:  # already rewritten in this change set: replace in place
                old.update(content)
                if taxonomy_version:
                    old["taxonomy_version"] = taxonomy_version
                return True
            self.close(fixture_table, old)
        row = {
            **content,
            key: fact_id,
            "revision_from": self.rev,
            "revision_to": None,
            "taxonomy_version": taxonomy_version or self.tax.version,
        }
        row.setdefault("trust_state", old["trust_state"] if old else "documented")  # set by the trust step
        self.add(fixture_table, row)
        current[fact_id] = row
        return True

    def entity_rows(self, items: list[EntityInput]) -> set[str]:
        changed: set[str] = set()
        for e in items:
            t = self.tax.types.get(e.type_key)
            content = {
                "org_id": self.scope.org_id,
                "type_key": e.type_key,
                "category": t["category"] if t else e.type_key,
                "provider": e.provider,
                "native_type": e.native_type,
                "record_state": e.record_state,
                "merged_into": e.merged_into,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "environment_scope": e.environment_scope,
                "environment_id": e.environment_id,
                "container_id": e.container_id,
                "attributes": e.attributes,
                **(e.verification or UNVERIFIED),
            }
            if e.entity_id not in self.entity_ids:
                self.entity_ids.add(e.entity_id)
                self.add("entities", {"org_id": self.scope.org_id, "id": e.entity_id, "created_revision": self.rev})
            if self._version(
                "entity_versions",
                "entity_id",
                e.entity_id,
                content,
                self.current_ev,
                taxonomy_version=e.taxonomy_version,
            ):
                changed.add(e.entity_id)
        return changed

    def relationship_rows(self, items: list[RelationshipInput]) -> set[str]:
        changed: set[str] = set()
        keys = {
            (r["rel_type"], r["from_id"], r["to_id"], r["discriminator"]): rid for rid, r in self.relationships.items()
        }
        for r in items:
            disc = discriminator_of(self.tax, r.rel_type, r.attributes) if r.rel_type in self.tax.rels else ""
            identity = {
                "org_id": self.scope.org_id,
                "id": r.relationship_id,
                "rel_type": r.rel_type,
                "from_id": r.from_id,
                "to_id": r.to_id,
                "discriminator": disc,
            }
            existing = self.relationships.get(r.relationship_id)
            if existing is None:
                other = keys.get((r.rel_type, r.from_id, r.to_id, disc))
                if other is not None:
                    self.reject("H-09", f"relationship {r.relationship_id}: the same fact already exists as {other}")
                    continue
                row = {**identity, "created_revision": self.rev}
                self.relationships[r.relationship_id] = row
                keys[(r.rel_type, r.from_id, r.to_id, disc)] = r.relationship_id
                self.add("relationships", row)
            elif {k: existing[k] for k in identity} != identity:
                self.reject(
                    "H-09", f"relationship {r.relationship_id}: type, endpoints and discriminator are immutable"
                )
                continue
            content = {
                "org_id": self.scope.org_id,
                "record_state": r.record_state,
                "lifecycle": r.lifecycle,
                "environment_id": None,
                "cross_environment": False,
                "shared_endpoint": False,
                "cross_provider": False,
                "attributes": r.attributes,
                **(r.verification or UNVERIFIED),
            }
            old = self.current_rv.get(r.relationship_id)
            if old is not None:  # flags are derived below; compare like with like
                content.update(
                    {k: old[k] for k in ("environment_id", "cross_environment", "shared_endpoint", "cross_provider")}
                )
            if self._version(
                "relationship_versions",
                "relationship_id",
                r.relationship_id,
                content,
                self.current_rv,
                taxonomy_version=r.taxonomy_version,
            ):
                changed.add(r.relationship_id)
        return changed

    # ---- derived fields
    def derive(
        self,
        entities: set[str],
        relationships: set[str],
        asserted: set[str],
        expected: dict[str, str],
        *,
        recompute_all: bool = False,
    ) -> None:
        """Flags for relationships touching changed entities, then trust for every touched fact (or every fact)."""
        touched_rel = set(relationships)
        for rid, r in self.relationships.items():
            if r["from_id"] in entities or r["to_id"] in entities:
                touched_rel.add(rid)
        for rid in sorted(touched_rel):
            v = self.current_rv.get(rid)
            r = self.relationships[rid]
            fv, tv = self.current_ev.get(r["from_id"]), self.current_ev.get(r["to_id"])
            if v is None or fv is None or tv is None or v["record_state"] != "present":
                continue
            flags = relationship_flags(fv, tv)
            if any(v[k] != val for k, val in flags.items()):
                self._version("relationship_versions", "relationship_id", rid, {**_strip(v), **flags}, self.current_rv)
        if recompute_all:  # time-driven re-evaluation of every current fact (trust-function.md, recomputation)
            entities, touched_rel = set(self.current_ev), set(self.current_rv)
        facts = [("entity", fid) for fid in sorted(entities | (asserted & set(self.current_ev)))] + [
            ("relationship", fid) for fid in sorted(touched_rel | (asserted & set(self.current_rv)))
        ]
        for kind, fid in facts:
            self._trust(kind, fid, expected.get(fid))

    def _trust(self, kind: str, fid: str, expected: str | None) -> None:
        current = self.current_ev if kind == "entity" else self.current_rv
        table, key = (
            ("entity_versions", "entity_id") if kind == "entity" else ("relationship_versions", "relationship_id")
        )
        v = current.get(fid)
        if v is None:
            return
        if v["record_state"] != "present":
            self._tombstone_trust(table, key, fid, v, current)
            return
        claim = self._claim(kind, fid, v)
        if claim is None:
            return  # unknown type or missing endpoint: the domain rules report it
        assertions = [
            {
                **a,
                "method": self.evidence.get(a["evidence_id"], {}).get("method"),
                "observed_at": self.evidence.get(a["evidence_id"], {}).get("observed_at"),
                "valid_to": self.evidence.get(a["evidence_id"], {}).get("valid_to"),
            }
            for a in self.asserts_by_fact.get(fid, [])
            if valid_at(a, self.rev)
        ]
        prev = next((p for p in self.closed.get(table, []) if p[key] == fid), None) or (
            v if v["revision_from"] < self.rev else None
        )
        prior = {
            a["field_path"]: rules.field_value(prev, a["field_path"]) for a in assertions if prev and a["field_path"]
        }
        result = _evaluate(claim, assertions, v, self.ds.policy, self.at, prior)
        if result is None:
            return  # TF-1: reported by D-12 during validation
        if expected is not None and expected != result:
            self.reject("D-12", f"{kind} {fid}: expected trust state {expected}, the trust function gives {result}")
        if v["trust_state"] != result:
            self._version(table, key, fid, {**_strip(v), "trust_state": result}, current, with_trust=True)

    def _claim(self, kind: str, fid: str, v: Row) -> Claim | None:
        """The claim as of this change set: endpoints are resolved from the plan's current versions, which include
        the versions this change set writes (an index built earlier would no longer see them)."""
        if kind == "entity":
            return Claim("entity", self.tax.is_physical(v["type_key"]))
        r = self.relationships[fid]
        ends = [self.current_ev.get(r["from_id"]), self.current_ev.get(r["to_id"])]
        rule = self.tax.rels.get(r["rel_type"])
        if rule is None or ends[0] is None or ends[1] is None:
            return None
        physical = any(self.tax.is_physical(e["type_key"]) for e in ends if e is not None)
        return Claim("relationship", physical, frozenset(rule["allowed_evidence_kinds"]))

    def _tombstone_trust(self, table: str, key: str, fid: str, v: Row, current: dict[str, Row]) -> None:
        """A removal or merge records how the removal is known: the label of its ``record_state`` evidence."""
        kinds = {
            a["evidence_kind"]
            for a in self.asserts_by_fact.get(fid, [])
            if a["field_path"] == "record_state" and valid_at(a, self.rev)
        }
        label = next((state for kind, state in TOMBSTONE_TRUST if kind in kinds), None)
        if label is not None and v["trust_state"] != label:
            self._version(table, key, fid, {**_strip(v), "trust_state": label}, current, with_trust=True)


def _strip(v: Row) -> Row:
    return {k: x for k, x in v.items() if k not in REVISION_FIELDS}


def _evaluate(claim: Claim, assertions: list[Row], v: Row, policy: dict[str, Any], at: str, prior: Row) -> str | None:
    return evaluate(claim, assertions, v, policy, at, prior).trust_state


def _write(conn: Connection, plan: _Plan, change_set: Row) -> PublishResult:
    rel_types = {rid: r["rel_type"] for rid, r in plan.relationships.items()}
    conn.execute(insert_sql("change_set"), to_db("change_set", change_set))
    closing = {
        "entity_versions": ("entity_version", "entity_id"),
        "relationship_versions": ("relationship_version", "relationship_id"),
        "external_identities": ("external_identity", "id"),
        "fact_assertions": ("fact_assertion", "id"),
    }
    closed: dict[str, int] = {}
    for fixture_table, (table, key) in closing.items():
        rows = [r for r in plan.closed.get(fixture_table, []) if r["revision_from"] < plan.rev]
        versioned = fixture_table.endswith("versions")
        sql = (
            f"UPDATE atlas.{table} SET revision_to = %s WHERE org_id = %s AND {key} = %s "  # noqa: S608 - constants
            + ("AND revision_from = %s " if versioned else "")
            + "AND revision_to IS NULL"
        )
        for r in rows:
            params = (plan.rev, plan.scope.org_id, r[key], *([r["revision_from"]] if versioned else []))
            if conn.execute(sql, params).rowcount != 1:
                raise IntegrityCheckError(f"{table} {r[key]} was not current when closing it")
        closed[table] = len(rows)
    order = (
        "evidence",
        "entities",
        "entity_versions",
        "relationships",
        "relationship_versions",
        "external_identities",
        "fact_assertions",
    )
    written: dict[str, int] = {}
    for fixture_table in order:
        rows = plan.new.get(fixture_table, [])
        if not rows:
            continue
        table = FIXTURE_TABLE[fixture_table]
        with conn.cursor() as cur:
            cur.executemany(insert_sql(table), [to_db(table, r, rel_types=rel_types) for r in rows])
        written[table] = len(rows)
    return PublishResult(plan.rev, written, closed)


def publish(
    conn: Connection,
    scope: OrgScope,
    actor: Actor,
    draft: ChangeSetDraft,
    taxes: dict[str, Taxonomy],
    policy: dict[str, Any] | None = None,
    *,
    taxonomy_version: str | None = None,
    evaluated_at: str | None = None,
    recompute: bool = False,
) -> PublishResult:
    """Publish ``draft`` as one change set of ``scope``. Raises :class:`PublishRejected` with readable reasons.

    ``taxonomy_version`` (default: the approved one) validates and stamps the new rows; it must be loaded in ``taxes``.
    ``evaluated_at`` replaces the transaction time as the trust function's evaluation time when recorded history is
    replayed (the fixture loader): the time of the recorded change set, never later than now. ``recompute`` re-evaluates
    the trust of every current fact (a recompute change set).
    """
    tax = taxes[taxonomy_version] if taxonomy_version else rules.base_taxonomy(taxes)
    if (draft.kind == "revert") != (draft.reverts_revision is not None):
        raise PublishRejected(
            [rules.Finding("H-05", rules.HARD, "a revert names the revision it reverts (and only a revert)")]
        )
    with conn.transaction():
        row = conn.execute(
            "SELECT last, now() FROM atlas.org_revision WHERE org_id = %s FOR UPDATE", (scope.org_id,)
        ).fetchone()
        if row is None:
            raise repository.OrganizationNotFound(scope.key)
        revision, at = int(row[0]) + 1, format_ts(row[1])
        assert at is not None  # noqa: S101 - now() is never NULL
        if evaluated_at is not None:
            if timestamp(evaluated_at) > timestamp(at):
                raise PublishRejected([rules.Finding("H-05", rules.HARD, "evaluated_at is later than the server time")])
            at = evaluated_at
        base = repository.load(conn, scope, history=draft.kind == "revert")  # D-14 needs the reverted history
        base.policy = copy.deepcopy(policy if policy is not None else NO_POLICY)
        # evidence the draft names, already stored but not referenced by a current assertion, is loaded too:
        # a resubmitted evidence row is then recognised as unchanged instead of inserted twice
        missing = sorted(
            ({a.evidence_id for a in draft.assertions} | {e.evidence_id for e in draft.evidence})
            - {e["id"] for e in base.tables["evidence"]}
        )
        base.tables["evidence"] += repository.evidence_rows(conn, scope, missing)
        plan = _Plan(scope, base, tax, revision, at)
        plan.evidence_rows(draft.evidence)
        plan.identity_rows(draft.identities, draft.close_identities)
        changed_entities = plan.entity_rows(draft.entities)
        changed_relationships = plan.relationship_rows(draft.relationships)
        asserted = plan.assertion_rows(draft.assertions, draft.close_assertions)
        expected = {e.entity_id: e.expected_trust_state for e in draft.entities if e.expected_trust_state}
        expected |= {r.relationship_id: r.expected_trust_state for r in draft.relationships if r.expected_trust_state}
        plan.derive(changed_entities, changed_relationships, asserted, expected, recompute_all=recompute)
        if plan.findings:
            raise PublishRejected(plan.findings)
        if plan.empty:
            return PublishResult(None)
        change_set = {
            "org_id": scope.org_id,
            "revision": revision,
            "kind": draft.kind,
            "channel": draft.channel,
            "actor": {"kind": actor.kind, "namespace": actor.namespace, "key": actor.key, "label": actor.label},
            "source_ref": draft.source_ref,
            "input_hash": draft.input_hash,
            "reverts_revision": draft.reverts_revision,
            "reason": draft.reason,
            "change_reference": draft.change_reference,
            "created_at": at,
            "db_role": None,
        }
        plan.ds.tables["change_sets"].append(change_set)
        findings = rules.validate(plan.ds, taxes, {scope.org_id: {revision}})
        blocking = rules.blocking(findings)
        if blocking:
            raise PublishRejected(blocking)
        result = _write(conn, plan, change_set)
        problems = integrity.revision_findings(conn, scope, revision)
        if problems:
            raise IntegrityCheckError("; ".join(problems))
        conn.execute("UPDATE atlas.org_revision SET last = %s WHERE org_id = %s", (revision, scope.org_id))
        result.advisories = rules.advisory_findings(plan.ds, taxes)
        return result
