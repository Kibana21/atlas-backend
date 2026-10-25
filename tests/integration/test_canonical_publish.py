"""The publishing procedure on PostgreSQL 15, as the ingestion role (A002-FR-06/07/08, AC-CP3-06, AC-CP3-08).

Each test runs inside ``transaction(force_rollback=True)``; ``publish`` nests its own transaction as a savepoint, so
a rejected change set rolls back exactly as it would on its own, and nothing survives the test. The draft is the
small synthetic graph of the unit tests (``tests/unit/canonical_graph.py``), published as a fixture load.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg import errors
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.domain.canonical import taxonomy
from atlas.persistence.canonical import integrity, repository
from atlas.persistence.canonical.publish import (
    Actor,
    AssertionInput,
    ChangeSetDraft,
    EntityInput,
    EvidenceInput,
    IdentityInput,
    PublishRejected,
    RelationshipInput,
    publish,
)
from atlas.persistence.canonical.repository import OrgScope
from tests.integration.conftest import connect, tx_connect
from tests.unit import canonical_graph as g

pytestmark = [pytest.mark.integration, pytest.mark.security]
TAXES = {taxonomy.CURRENT_VERSION: taxonomy.current()}
LOADER = Actor("service", "atlas.service", "fixture-loader")
STEWARD = Actor("human", "oidc", g.STEWARD["key"])


def draft_from_graph(**overrides: Any) -> ChangeSetDraft:
    ds = g.build()
    t = ds.tables
    entities = [
        EntityInput(
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
            copy.deepcopy(v["attributes"]),
            {k: v[k] for k in ("verification_state", "verified_by", "verified_at", "expires_at")},
            expected_trust_state=v["trust_state"],
        )
        for v in t["entity_versions"]
    ]
    versions = {v["relationship_id"]: v for v in t["relationship_versions"]}
    relationships = [
        RelationshipInput(
            r["id"],
            r["rel_type"],
            r["from_id"],
            r["to_id"],
            copy.deepcopy(versions[r["id"]]["attributes"]),
            expected_trust_state=versions[r["id"]]["trust_state"],
        )
        for r in t["relationships"]
    ]
    fields: dict[str, Any] = {
        "kind": "fixture_load",
        "channel": "fixture_cli",
        "source_ref": "integration test",
        "reason": "synthetic graph",
        "evidence": [
            EvidenceInput(e["id"], e["kind"], e["source_ref"], e["method"], e["observed_at"]) for e in t["evidence"]
        ],
        "entities": entities,
        "relationships": relationships,
        "identities": [
            IdentityInput(i["id"], i["entity_id"], i["namespace"], i["original_key"], i["match_method"])
            for i in t["external_identities"]
        ],
        "assertions": [
            AssertionInput(a["id"], a["fact_kind"], a["fact_id"], a["evidence_id"]) for a in t["fact_assertions"]
        ],
    }
    return ChangeSetDraft(**{**fields, **overrides})


@pytest.fixture
def ingest(migrated: None, ingest_url: str) -> Iterator[tuple[psycopg.Connection[Any], OrgScope]]:
    with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
        yield conn, repository.resolve_scope(conn, "default")


def counts(conn: psycopg.Connection[Any], scope: OrgScope) -> tuple[int, int]:
    (cs,) = conn.execute("SELECT count(*) FROM atlas.change_set WHERE org_id = %s", (scope.org_id,)).fetchone() or (0,)
    return repository.current_revision(conn, scope), int(cs)


def test_publish_writes_one_change_set_and_a_replay_writes_nothing(
    ingest: tuple[psycopg.Connection[Any], OrgScope], settings: Settings
) -> None:
    conn, scope = ingest
    before = counts(conn, scope)
    result = publish(conn, scope, LOADER, draft_from_graph(), TAXES)
    assert result.revision == before[0] + 1
    assert result.written["entity_version"] == 7
    assert result.written["relationship_version"] == 5
    assert result.written["fact_assertion"] == 12
    assert result.advisories == []
    assert integrity.revision_findings(conn, scope, result.revision) == []
    assert counts(conn, scope) == (before[0] + 1, before[1] + 1)
    created_at, db_role, actor = conn.execute(
        "SELECT created_at, db_role, actor_namespace || ':' || actor_key FROM atlas.change_set "
        "WHERE org_id = %s AND revision = %s",
        (scope.org_id, result.revision),
    ).fetchone() or (None, None, None)
    assert created_at is not None
    assert db_role == settings.db_ingest_role
    assert actor == "atlas.service:fixture-loader"
    web = repository.entity_version(conn, scope, g.WEB)
    assert web is not None
    assert (web["trust_state"], web["verification_state"], web["revision_from"]) == (
        "discovered",
        "verified",
        result.revision,
    )
    deployed = conn.execute(
        "SELECT environment_id::text FROM atlas.relationship_version "
        "WHERE relationship_id = %s AND revision_to IS NULL",
        (g.uid(33),),
    ).fetchone()
    assert deployed == (g.ENV,)  # derived from the endpoints (ADR-0017 §4.4)

    replay = publish(conn, scope, LOADER, draft_from_graph(), TAXES)
    assert replay.revision is None  # no difference: no change set, no revision consumed
    assert counts(conn, scope) == (before[0] + 1, before[1] + 1)

    rename = draft_from_graph(kind="steward_edit", channel="api")
    team = next(e for e in rename.entities if e.entity_id == g.TEAM)
    team.display_name = "Claims Platform Team"
    second = publish(conn, scope, STEWARD, rename, TAXES)
    assert second.revision == result.revision + 1
    assert second.closed["entity_version"] == 1
    assert repository.entity_version(conn, scope, g.TEAM, result.revision)["display_name"] == "Claims Team"  # type: ignore[index]
    assert repository.entity_version(conn, scope, g.TEAM)["display_name"] == "Claims Platform Team"  # type: ignore[index]


def _without(entity_id: str) -> Callable[[ChangeSetDraft], None]:
    def mutate(d: ChangeSetDraft) -> None:
        d.assertions = [a for a in d.assertions if a.fact_id != entity_id]
        d.assertions.append(AssertionInput(g.uid(500), "entity", entity_id, g.EV_PROV))

    return mutate


def _entity(entity_id: str, **changes: Any) -> Callable[[ChangeSetDraft], None]:
    def mutate(d: ChangeSetDraft) -> None:
        e = next(x for x in d.entities if x.entity_id == entity_id)
        for k, v in changes.items():
            setattr(e, k, v)

    return mutate


def _relationship(
    rel_type: str, frm: str, to: str, evidence: str = g.EV_DOC, **attrs: Any
) -> Callable[[ChangeSetDraft], None]:
    def mutate(d: ChangeSetDraft) -> None:
        rid = g.uid(600)
        d.relationships.append(RelationshipInput(rid, rel_type, frm, to, dict(attrs)))
        d.assertions.append(AssertionInput(g.uid(601), "relationship", rid, evidence))

    return mutate


def _identity(entity_id: str, namespace: str, key: str) -> Callable[[ChangeSetDraft], None]:
    def mutate(d: ChangeSetDraft) -> None:
        d.identities.append(IdentityInput(g.uid(700), entity_id, namespace, key, "provider_id"))

    return mutate


def _verified_by_service(d: ChangeSetDraft) -> None:
    web = next(x for x in d.entities if x.entity_id == g.WEB)
    assert web.verification is not None
    web.verification = {**web.verification, "verified_by": {"namespace": "atlas.service", "key": "import-worker"}}


REJECTIONS: list[tuple[str, Callable[[ChangeSetDraft], None], str]] = [
    ("logical fact from provider configuration only", _without(g.TEAM), "D-12"),
    ("expected trust differs from the trust function", _entity(g.APP, expected_trust_state="curated"), "D-12"),
    ("verification by a service", _verified_by_service, "D-19"),
    ("missing required attribute", _entity(g.RG, attributes={}), "D-01"),
    ("reserved relationship type", _relationship("PROTECTS", g.WEB, g.RG), "D-04"),
    ("pair not in the matrix", _relationship("READS_FROM", g.TEAM, g.APP), "D-04"),
    ("self-relationship", _relationship("OWNS", g.TEAM, g.TEAM), "H-09"),
    ("second containment parent", _relationship("CONTAINS", g.SUB, g.WEB, g.EV_PROV), "H-14"),
    ("malformed provider key", _identity(g.WEB, "azure.arm", "subscriptions/not-an-arm-id"), "H-08"),
    ("key already bound to another entity", _identity(g.WEB, "azure.arm", g.ARM_RG), "H-07"),
    (
        "environment of another organization",
        _entity(g.WEB, environment_id="0190f000-0000-7000-8000-999999999999"),
        "H-04",
    ),
]


@pytest.mark.parametrize(("label", "mutate", "rule"), REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_invalid_change_sets_are_rejected_with_their_rule(
    ingest: tuple[psycopg.Connection[Any], OrgScope], label: str, mutate: Callable[[ChangeSetDraft], None], rule: str
) -> None:
    conn, scope = ingest
    before = counts(conn, scope)
    draft = draft_from_graph()
    mutate(draft)
    with pytest.raises(PublishRejected) as caught:
        publish(conn, scope, LOADER, draft, TAXES)
    assert rule in caught.value.rule_ids, (label, str(caught.value))
    assert counts(conn, scope) == before  # nothing written, no revision consumed


def test_attribution_must_fit_the_channel(ingest: tuple[psycopg.Connection[Any], OrgScope]) -> None:
    conn, scope = ingest
    with pytest.raises(PublishRejected) as caught:
        publish(conn, scope, STEWARD, draft_from_graph(), TAXES)  # a human on the fixture channel
    assert "D-13" in caught.value.rule_ids
    with pytest.raises(PublishRejected) as caught:
        publish(conn, scope, LOADER, draft_from_graph(kind="revert"), TAXES)
    assert "D-14" in caught.value.rule_ids  # reverts are CP4 work


def test_cross_check_the_dependent_relationship_evidence(ingest: tuple[psycopg.Connection[Any], OrgScope]) -> None:
    conn, scope = ingest
    draft = draft_from_graph()
    _relationship("DEPENDS_ON", g.APP, g.TEAM, g.EV_PROV, rationale="x")(draft)
    with pytest.raises(PublishRejected) as caught:
        publish(conn, scope, LOADER, draft, TAXES)
    assert {"D-04", "D-05"} & caught.value.rule_ids


def test_the_runtime_role_cannot_publish(migrated: None, settings: Settings) -> None:
    with tx_connect(settings.database_url) as conn, conn.transaction(force_rollback=True):
        scope = repository.resolve_scope(conn, "default")
        with pytest.raises(errors.InsufficientPrivilege):
            publish(conn, scope, LOADER, draft_from_graph(), TAXES)


def test_other_organizations_are_not_found(migrated: None, settings: Settings, ingest_url: str) -> None:
    other = "0190f000-0000-7000-8000-000000000999"
    with connect(settings.migration_database_url) as owner:
        owner.execute(
            "INSERT INTO atlas.organization (id, key, display_name) VALUES (%s, 'cp3-other', 'Other')", (other,)
        )
        owner.execute("INSERT INTO atlas.org_revision (org_id, last) VALUES (%s, 0)", (other,))
    try:
        with tx_connect(ingest_url) as conn, conn.transaction(force_rollback=True):
            default = repository.resolve_scope(conn, "default")
            assert publish(conn, default, LOADER, draft_from_graph(), TAXES).revision is not None
            scope = repository.resolve_scope(conn, "cp3-other")
            assert repository.entity_version(conn, scope, g.TEAM) is None  # exists only in "default"
            assert repository.load(conn, scope).tables["entities"] == []
            with pytest.raises(repository.OrganizationNotFound):
                repository.resolve_scope(conn, "no-such-organization")
            draft = ChangeSetDraft("fixture_load", "fixture_cli", "test", "cross-organization reference")
            draft.entities.append(EntityInput(g.uid(800), "core.service", "Svc", container_id=g.APP))
            draft.assertions.append(AssertionInput(g.uid(801), "entity", g.uid(800), g.EV_DOC))
            with pytest.raises(PublishRejected) as caught:
                publish(conn, scope, LOADER, draft, TAXES)
            assert "H-04" in caught.value.rule_ids  # the other organization's evidence and entity do not resolve
    finally:
        with connect(settings.migration_database_url) as owner:
            owner.execute("DELETE FROM atlas.org_revision WHERE org_id = %s", (other,))
            owner.execute("DELETE FROM atlas.organization WHERE id = %s", (other,))
    assert make_url(ingest_url).username == settings.db_ingest_role
