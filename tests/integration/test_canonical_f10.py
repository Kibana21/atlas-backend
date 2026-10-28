"""F-10 regression tests (atlas_test only, every write rolled back): validation with a partial taxonomy set.

The golden history stamps r7's re-typed data store and r8's READS_FROM with the synthetic taxonomy 1.2.0-test.1. A
caller that loads only the approved taxonomy 1.1.0 validates against a partial set. The D-04 re-check of an existing
relationship after a re-type needs that relationship's recorded taxonomy: without it the change set is refused with
D-01 (unknown taxonomy version) and nothing is committed (canonical-validation-rules.md D-04, F-10).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from atlas.domain.canonical import rules, taxonomy
from atlas.persistence.canonical import history, integrity, repository
from atlas.persistence.canonical import publish as publish_module
from atlas.persistence.canonical.publish import PublishRejected, publish
from tests.integration.conftest import tx_connect
from tests.integration.test_canonical_cp4 import cp4_orgs, g_steward, golden  # noqa: F401 - cp4_orgs is a fixture

pytestmark = pytest.mark.integration
APPROVED_ONLY = {taxonomy.CURRENT_VERSION: taxonomy.current()}


@pytest.fixture(scope="module")
def replayed(request: pytest.FixtureRequest, ingest_url: str) -> Iterator[psycopg.Connection[Any]]:
    """One ingestion connection with the golden history (r1..r8, every taxonomy loaded) replayed once into
    cp4-golden, inside a transaction that is always rolled back (organizations from cp4_orgs)."""
    request.getfixturevalue("cp4_orgs")
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    with tx_connect(ingest_url) as c, c.transaction(force_rollback=True):
        history.replay(c, repository.resolve_scope(c, "cp4-golden"), ds, source, taxes, policy)
        yield c


@pytest.fixture
def conn(replayed: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """Each test runs in a savepoint over the replayed history that is always rolled back."""
    with replayed.transaction(force_rollback=True):
        yield replayed


def _golden_head(conn: psycopg.Connection[Any]) -> tuple[repository.OrgScope, dict[str, taxonomy.Taxonomy]]:
    return repository.resolve_scope(conn, "cp4-golden"), golden()[1]


def _warehouse(conn: psycopg.Connection[Any], scope: repository.OrgScope) -> dict[str, Any]:
    rows = repository.load(conn, scope).tables["entity_versions"]
    return next(v for v in rows if v["display_name"] == "analytics-warehouse")


def _retype_to_resource(conn: psycopg.Connection[Any], scope: repository.OrgScope) -> publish_module.ChangeSetDraft:
    v = _warehouse(conn, scope)
    draft = publish_module.ChangeSetDraft("steward_edit", "api", "re-type", "steward re-type to infra.resource")
    draft.entities.append(history.entity_input({**v, "type_key": "infra.resource", "category": "infra.resource"}))
    return draft


def _full_findings(conn: psycopg.Connection[Any], scope: repository.OrgScope, taxes: dict[str, Any], rev: int) -> Any:
    """Re-validate revision ``rev`` from the database with every taxonomy loaded (what the rules require)."""
    ds = repository.load(conn, scope, history=True)
    return {f.rule for f in rules.validate(ds, taxes, {scope.org_id: {rev}})}


# --------------------------------------------------------------------------- replay (runs before the module replay)


def test_replay_needs_the_recorded_taxonomy_and_fails_explicitly_without_it(
    request: pytest.FixtureRequest, ingest_url: str
) -> None:
    """Its own rolled-back connection: the fixture's IDs are global keys, so it must run before ``replayed`` exists
    (first in this file). ``lock_timeout`` turns a reordering into a fast failure instead of a lock wait."""
    request.getfixturevalue("cp4_orgs")
    ds, _, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    with tx_connect(ingest_url) as c, c.transaction(force_rollback=True):
        c.execute("SET LOCAL lock_timeout = '5s'")
        scope = repository.resolve_scope(c, "cp4-golden")
        assert repository.current_revision(c, scope) == 0
        with pytest.raises(KeyError, match=r"1\.2\.0-test\.1"):
            history.replay(c, scope, ds, source, APPROVED_ONLY, policy)


# --------------------------------------------------------------------------- reference: every taxonomy loaded


def test_with_every_taxonomy_loaded_both_paths_are_refused_by_d04(conn: psycopg.Connection[Any]) -> None:
    scope, taxes = _golden_head(conn)
    with pytest.raises(PublishRejected) as revert:
        history.revert(conn, scope, g_steward(), 7, taxes)
    assert revert.value.rule_ids == {"D-04"}
    with pytest.raises(PublishRejected) as edit:
        publish(conn, scope, g_steward(), _retype_to_resource(conn, scope), taxes)
    assert "D-04" in edit.value.rule_ids
    assert repository.current_revision(conn, scope) == 8


# --------------------------------------------------------------------------- F-10: partial set, explicit refusal


def _refused_with_d01(
    conn: psycopg.Connection[Any], scope: repository.OrgScope, taxes: dict[str, Any], exc: PublishRejected
) -> None:
    """D-01 from the re-check (never a silent skip), and no revision committed: r8 stays the valid head."""
    assert exc.rule_ids == {"D-01"}
    assert all("re-typed" in str(f) and "unknown taxonomy version 1.2.0-test.1" in str(f) for f in exc.findings)
    assert repository.current_revision(conn, scope) == 8
    assert integrity.revision_findings(conn, scope, 8) == []
    assert integrity.history_findings(conn, scope) == []
    assert _full_findings(conn, scope, taxes, 8) == set()


def test_revert_service_refuses_when_a_needed_taxonomy_is_not_loaded(conn: psycopg.Connection[Any]) -> None:
    scope, taxes = _golden_head(conn)
    with pytest.raises(PublishRejected) as refused:
        history.revert(conn, scope, g_steward(), 7, APPROVED_ONLY)
    _refused_with_d01(conn, scope, taxes, refused.value)


def test_direct_publish_refuses_a_retype_when_a_needed_taxonomy_is_not_loaded(
    conn: psycopg.Connection[Any],
) -> None:
    scope, taxes = _golden_head(conn)
    with pytest.raises(PublishRejected) as refused:
        publish(conn, scope, g_steward(), _retype_to_resource(conn, scope), APPROVED_ONLY)
    _refused_with_d01(conn, scope, taxes, refused.value)


# --------------------------------------------------------------------------- already explicit, or legitimate


def test_a_revert_restoring_an_unloaded_taxonomy_version_is_refused_with_d01(
    conn: psycopg.Connection[Any],
) -> None:
    """Restoring a version stamped 1.2.0-test.1 needs that taxonomy: refused explicitly (D-01), nothing written."""
    scope, taxes = _golden_head(conn)
    v = _warehouse(conn, scope)
    rename = publish_module.ChangeSetDraft("steward_edit", "api", "rename", "steward rename")
    rename.entities.append(history.entity_input({**v, "display_name": "analytics-warehouse-eu"}))
    assert publish(conn, scope, g_steward(), rename, taxes).revision == 9
    with pytest.raises(PublishRejected) as refused:
        history.revert(conn, scope, g_steward(), 9, APPROVED_ONLY)
    assert refused.value.rule_ids == {"D-01"}
    assert "unknown taxonomy version 1.2.0-test.1" in str(refused.value)
    assert repository.current_revision(conn, scope) == 9


def test_historical_reconstruction_needs_no_taxonomy(conn: psycopg.Connection[Any]) -> None:
    """As-of reconstruction and graph hashes read stored rows only: the recorded taxonomy versions are data."""
    scope, _ = _golden_head(conn)
    expected = {e["revision"]: e["graph_hash"] for e in golden_expected()}
    assert {r: history.hash_at(conn, scope, r)[2] for r in expected} == expected
    stamped = {v["taxonomy_version"] for v in repository.load(conn, scope, history=True).tables["entity_versions"]}
    assert stamped == {"1.1.0", "1.2.0-test.1"}


def test_trust_recomputation_with_the_approved_taxonomy_only(conn: psycopg.Connection[Any]) -> None:
    """Recomputation re-types nothing (no D-04/D-10 path). Records what it does with a partial set."""
    scope, taxes = _golden_head(conn)
    result = history.recompute_trust(conn, scope, APPROVED_ONLY)
    if result.revision is not None:
        assert _full_findings(conn, scope, taxes, result.revision) == set()


def golden_expected() -> list[dict[str, Any]]:
    import json

    from tests.integration.test_canonical_cp4 import FIXTURE

    rows: list[dict[str, Any]] = json.loads((FIXTURE / "expected-as-of.json").read_text(encoding="utf-8"))
    return [e for e in rows if e["org_key"] == "default"]
