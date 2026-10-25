"""Canonical store entry points (ATLAS-002 CP4): the fixture loader and the import, revert and recompute commands.

    python -m atlas.tools.canonical replay  --org KEY --fixture DIR      # recorded history (golden fixture)
    python -m atlas.tools.canonical import  --org KEY --batch FILE       # import batch + report
    python -m atlas.tools.canonical revert  --org KEY --revision N
    python -m atlas.tools.canonical recompute-trust --org KEY
    python -m atlas.tools.canonical snapshot --org KEY --label NAME

Every command connects with the **ingestion** connection only (``ATLAS_INGEST_DATABASE_URL``, or the single
account in the local single-user model) and acts as a configured service or system identity (ADR-0008 §10). Nothing
here reads an actor from the input files. Output is a JSON summary without connection details.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from atlas.core.settings import Settings, get_settings
from atlas.domain.canonical import rules, taxonomy
from atlas.domain.canonical.graph import TABLES, Dataset
from atlas.persistence.canonical import history, repository
from atlas.persistence.canonical.publish import Actor, PublishRejected, PublishResult

STEWARD_REVERT = Actor("system", "atlas.system", "revert", "Revert command")


def load_fixture(directory: Path) -> tuple[Dataset, dict[str, taxonomy.Taxonomy]]:
    tables = {t: json.loads((directory / f"{t}.json").read_text(encoding="utf-8")) for t in TABLES}
    policy_path = directory / "trust-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else None
    base = taxonomy.current()
    taxes = {base.version: base}
    for path in sorted(directory.glob("taxonomy-overlay.*.json")):
        overlay = base.with_overlay(json.loads(path.read_text(encoding="utf-8")))
        taxes[overlay.version] = overlay
    ds = Dataset(tables)
    if policy is not None:
        ds.policy = policy
    return ds, taxes


def _summary(result: PublishResult) -> dict[str, Any]:
    return {
        "revision": result.revision,
        "written": result.written,
        "closed": result.closed,
        "advisories": rules.summary(result.advisories),
    }


def run(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    url = settings.ingestion_url
    if url is None:
        raise SystemExit(
            "no ingestion connection configured (ATLAS_INGEST_DATABASE_URL, or ATLAS_DB_ROLE_MODEL=single)"
        )
    taxes = {taxonomy.CURRENT_VERSION: taxonomy.current()}
    with psycopg.connect(repository.libpq_url(url), application_name="atlas-canonical-cli") as conn:
        scope = repository.resolve_scope(conn, args.org)
        if args.command == "replay":
            ds, taxes = load_fixture(Path(args.fixture))
            source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == (args.source_org or args.org))
            results = history.replay(conn, scope, ds, source, taxes, ds.policy)
            return {
                "command": "replay",
                "revisions": [_summary(r) for r in results],
                "graph": [history.hash_at(conn, scope, r.revision or 0) for r in results],
            }
        if args.command == "import":
            batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
            outcome = history.apply_import(conn, scope, batch, taxes)
            return {"command": "import", **_summary(outcome.result), "rejected": outcome.plan.report()}
        if args.command == "revert":
            return {
                "command": "revert",
                **_summary(history.revert(conn, scope, STEWARD_REVERT, args.revision, taxes, channel="system_job")),
            }
        if args.command == "recompute-trust":
            return {"command": "recompute-trust", **_summary(history.recompute_trust(conn, scope, taxes))}
        row = history.create_snapshot(conn, scope, args.label)
        return {
            "command": "snapshot",
            "label": row["label"],
            "revision": row["revision"],
            "graph_hash": row["graph_hash"],
        }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas-canonical", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="replay a golden fixture's recorded history into an empty organization")
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--source-org", help="organization key inside the fixture (default: --org)")
    imp = sub.add_parser("import", help="plan and publish an import batch")
    imp.add_argument("--batch", required=True)
    rev = sub.add_parser("revert", help="revert one revision as a new change set")
    rev.add_argument("--revision", type=int, required=True)
    sub.add_parser("recompute-trust", help="re-evaluate trust for every current fact")
    snap = sub.add_parser("snapshot", help="create a named, immutable snapshot of the current revision")
    snap.add_argument("--label", required=True)
    for s in (replay, imp, rev, sub.choices["recompute-trust"], snap):
        s.add_argument("--org", required=True, help="organization key")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        out = run(args, get_settings())
    except PublishRejected as exc:
        print(json.dumps({"rejected": [str(f) for f in exc.findings]}, indent=2))
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
