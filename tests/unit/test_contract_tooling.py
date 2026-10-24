"""Pending-operation subset and deterministic export (FR-36, AC-03)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.contract_subset import PendingError, main, subset
from scripts.export_openapi import export

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

CONTRACT: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"version": "0.1.0"},
    "paths": {
        "/a": {
            "get": {
                "operationId": "getA",
                "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}}},
            }
        },
        "/b": {
            "get": {
                "operationId": "getB",
                "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/B"}}}}},
            }
        },
    },
    "components": {"schemas": {"A": {"type": "object"}, "B": {"type": "object"}}},
}
ENTRY = {"operation_id": "getB", "method": "GET", "path": "/b", "lands_in": "B-09", "components": ["B"]}


def test_subset_removes_only_the_pending_operation_and_components() -> None:
    out = subset(CONTRACT, [ENTRY])
    assert set(out["paths"]) == {"/a"}
    assert set(out["components"]["schemas"]) == {"A"}
    assert CONTRACT["paths"]["/b"]  # input untouched


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**ENTRY, "path": "/missing"}, "is not in the contract"),
        ({**ENTRY, "operation_id": "getWrong"}, "has operationId"),
        ({k: v for k, v in ENTRY.items() if k != "lands_in"}, "lacks"),
        ({**ENTRY, "components": ["Nope"]}, "pending component"),
        ({**ENTRY, "components": ["A"]}, "still referenced"),
    ],
)
def test_invalid_pending_entries_are_rejected(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(PendingError, match=message):
        subset(copy.deepcopy(CONTRACT), [entry])


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    contract, pending, out = tmp_path / "c.yaml", tmp_path / "p.yaml", tmp_path / "o.json"
    contract.write_text(yaml.safe_dump(CONTRACT))
    pending.write_text(yaml.safe_dump({"pending_operations": [ENTRY]}))
    assert main([str(contract), str(pending), str(out)]) == 0
    assert "lands in B-09" in capsys.readouterr().out
    assert set(json.loads(out.read_text())["paths"]) == {"/a"}
    pending.write_text("pending_operations: []\n")
    assert main([str(contract), str(pending), str(out)]) == 0
    assert "full contract compared" in capsys.readouterr().out
    pending.write_text(yaml.safe_dump({"pending_operations": [{**ENTRY, "path": "/zzz"}]}))
    assert main([str(contract), str(pending), str(out)]) == 1


def test_repository_pending_list_is_well_formed() -> None:
    entries = yaml.safe_load((ROOT / "contract-pending.yaml").read_text())["pending_operations"]
    # B-18 (brought forward to CP4): every contract operation is implemented; the list stays empty.
    assert entries == []


def test_export_is_deterministic_and_matches_dialect() -> None:
    first, second = export(), export()
    assert first == second
    spec = json.loads(first)
    assert spec["info"]["version"] == "0.1.0"
    assert {op["operationId"] for item in spec["paths"].values() for op in item.values()} == {
        "getHealthLive",
        "getHealthReady",
        "getDiagnosticsBuild",
    }
