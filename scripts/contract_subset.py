"""Derive the compared contract: the pinned contract minus explicitly pending operations (FR-36).

Usage: python scripts/contract_subset.py PINNED_CONTRACT PENDING_YAML OUTPUT_JSON

Fails if a pending entry is not in the contract, has no landing task, or if a removed component
is still referenced by a remaining operation; so the list can never hide unrelated drift.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml


class PendingError(ValueError):
    pass


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(_refs(v) for k, v in node.items() if k != "$ref"))
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node))
    return set()


def subset(contract: dict[str, Any], pending: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(contract)
    schemas: dict[str, Any] = out.get("components", {}).get("schemas", {})
    removed_components: set[str] = set()
    for entry in pending:
        missing = {"operation_id", "method", "path", "lands_in", "components"} - set(entry)
        if missing:
            raise PendingError(f"pending entry {entry.get('operation_id')!r} lacks {sorted(missing)}")
        path, method = entry["path"], str(entry["method"]).lower()
        operation = out.get("paths", {}).get(path, {}).get(method)
        if operation is None:
            raise PendingError(f"pending {entry['operation_id']} ({method.upper()} {path}) is not in the contract")
        if operation.get("operationId") != entry["operation_id"]:
            raise PendingError(
                f"{method.upper()} {path} has operationId {operation.get('operationId')!r}, "
                f"not {entry['operation_id']!r}"
            )
        del out["paths"][path][method]
        if not out["paths"][path]:
            del out["paths"][path]
        for name in entry["components"]:
            if name not in schemas:
                raise PendingError(f"pending component {name!r} is not in the contract")
            removed_components.add(name)
    for name in removed_components:
        del schemas[name]
    dangling = {r.rsplit("/", 1)[-1] for r in _refs(out)} & removed_components
    if dangling:
        raise PendingError(f"removed components still referenced by remaining operations: {sorted(dangling)}")
    return out


def main(argv: list[str]) -> int:
    contract_path, pending_path, output_path = map(Path, argv)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    pending = (yaml.safe_load(pending_path.read_text(encoding="utf-8")) or {}).get("pending_operations") or []
    try:
        result = subset(contract, pending)
    except PendingError as exc:
        print(f"PENDING_LIST_INVALID: {exc}", file=sys.stderr)
        return 1
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for entry in pending:
        print(
            f"pending (not compared): {entry['operation_id']} {entry['method']} {entry['path']} "
            f"-> lands in {entry['lands_in']}"
        )
    if not pending:
        print("pending: none - full contract compared")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
