"""Publication benchmark at increasing graph sizes (ATLAS-002 CP4: A2-T20, A2-T25; A002-FR-12).

CI or disposable databases only: it refuses to run outside GitHub Actions unless ``--disposable`` is given, and it
never runs against the shared local server (ADR-0001: Scale A data never on the shared server).

For each size it creates an organization (as the migration connection), generates a synthetic, valid graph of
N entities and about 4N relationships (logical applications, services, APIs and datasets; one document as
evidence), and measures through the real publishing procedure:

* the initial load (one change set);
* an incremental change (rename one service): the publishing procedure loads the organization's whole current state
  (CP3-D7), so this shows what that costs at each size;
* a no-op republication of the same rename (no change set);
* the organization-scoped state load and the graph hash at head.

    python scripts/benchmark_canonical.py --sizes 1000,5000,10000 --out build/benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import psycopg

from atlas.core.settings import Settings
from atlas.domain.canonical import taxonomy
from atlas.persistence.canonical import history, repository
from atlas.persistence.canonical.publish import (
    AssertionInput,
    ChangeSetDraft,
    EntityInput,
    EvidenceInput,
    RelationshipInput,
    publish,
)

TAXES = {taxonomy.CURRENT_VERSION: taxonomy.current()}
OBSERVED = "2026-10-01T00:00:00Z"


def uid(org: int, n: int) -> str:
    return f"0190f{org:03x}-0000-7000-8000-{n:012d}"


def synthetic_draft(org: int, entities: int) -> ChangeSetDraft:
    """N entities in units of four (application, service, API, dataset) and about 4N relationships."""
    ev = uid(org, 1)
    draft = ChangeSetDraft("fixture_load", "fixture_cli", "benchmark", f"synthetic {entities}")
    draft.evidence.append(EvidenceInput(ev, "documentation", "synthetic architecture document", "bench:v1", OBSERVED))
    seq = [10]

    def next_id() -> str:
        seq[0] += 1
        return uid(org, seq[0])

    def entity(type_key: str, name: str, **kw: Any) -> str:
        eid = next_id()
        draft.entities.append(EntityInput(eid, type_key, name, **kw))
        draft.assertions.append(AssertionInput(next_id(), "entity", eid, ev))
        return eid

    def rel(rel_type: str, frm: str, to: str, **attrs: Any) -> None:
        rid = next_id()
        draft.relationships.append(RelationshipInput(rid, rel_type, frm, to, dict(attrs)))
        draft.assertions.append(AssertionInput(next_id(), "relationship", rid, ev))

    team = entity("core.team", "Benchmark Team")
    units = max(entities // 4, 16)  # >= 9 keeps every modular link distinct
    systems = [entity("core.system", f"system-{s}") for s in range(max(units // 50, 1))]
    apps, svcs, apis, data = [], [], [], []
    for u in range(units):
        a = entity("core.application", f"app-{u}", attributes={"org_key": f"BENCH-{u}"})
        s = entity("core.service", f"svc-{u}", container_id=a)
        apps.append(a)
        svcs.append(s)
        apis.append(entity("core.api", f"api-{u}", attributes={"api_version": "1"}))
        data.append(entity("core.dataset", f"data-{u}"))
    for u in range(units):
        a, s = apps[u], svcs[u]
        rel("CONTAINS", a, s)
        rel("EXPOSES_API", s, apis[u])
        rel("OWNS", team, a)
        rel("MEMBER_OF", a, systems[u % len(systems)])
        for k in range(3):
            rel("READS_FROM", s, data[(u + k) % units])
        rel("WRITES_TO", s, data[u])
        for k in range(1, 6):
            rel("CONSUMES_API", s, apis[(u + k) % units])
        for k in range(1, 4):
            rel("CONSUMES_API", a, apis[(u + k + 5) % units])
    return draft


def timed(fn: Any) -> tuple[Any, float]:
    start = time.perf_counter()
    out = fn()
    return out, round(time.perf_counter() - start, 3)


def run_size(settings: Settings, org: int, size: int) -> dict[str, Any]:
    key = f"bench-{size}"
    with psycopg.connect(repository.libpq_url(settings.migration_database_url), autocommit=True) as owner:
        org_id = uid(org, 0)
        owner.execute("INSERT INTO atlas.organization (id, key, display_name) VALUES (%s, %s, %s)", (org_id, key, key))
        owner.execute("INSERT INTO atlas.org_revision (org_id, last) VALUES (%s, 0)", (org_id,))
    url = settings.ingestion_url
    assert url is not None  # noqa: S101 - checked by main()
    draft = synthetic_draft(org, size)
    with psycopg.connect(repository.libpq_url(url)) as conn:
        scope = repository.resolve_scope(conn, key)
        initial, t_initial = timed(lambda: publish(conn, scope, history.FIXTURE_LOADER, draft, TAXES))
        conn.commit()
        rename = synthetic_draft(org, size)
        svc = next(e for e in rename.entities if e.type_key == "core.service")
        svc.display_name = "svc-renamed"
        incremental, t_incremental = timed(lambda: publish(conn, scope, history.FIXTURE_LOADER, rename, TAXES))
        conn.commit()
        noop, t_noop = timed(lambda: publish(conn, scope, history.FIXTURE_LOADER, rename, TAXES))
        loaded, t_load = timed(lambda: repository.load(conn, scope))
        graph, t_hash = timed(lambda: history.hash_at(conn, scope, repository.current_revision(conn, scope)))
    return {
        "entities": len(draft.entities),
        "relationships": len(draft.relationships),
        "assertions": len(draft.assertions),
        "initial_publish_s": t_initial,
        "incremental_publish_s": t_incremental,
        "noop_publish_s": t_noop,
        "state_load_s": t_load,
        "graph_hash_s": t_hash,
        "revisions": [initial.revision, incremental.revision, noop.revision],
        "loaded_rows": sum(len(v) for v in loaded.tables.values()),
        "head": {"entities": graph[0], "relationships": graph[1]},
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--sizes", default="1000,5000,10000")
    p.add_argument("--out", default="build/benchmark-canonical.json")
    p.add_argument("--disposable", action="store_true", help="the database is disposable (outside GitHub Actions)")
    args = p.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.disposable:
        print("refusing to run: CI or a disposable database only (never the shared local server)", file=sys.stderr)
        return 2
    settings = Settings(_env_file=None)
    if settings.ingestion_url is None:
        print("no ingestion connection configured", file=sys.stderr)
        return 2
    results = [run_size(settings, i + 1, int(n)) for i, n in enumerate(args.sizes.split(","))]
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
