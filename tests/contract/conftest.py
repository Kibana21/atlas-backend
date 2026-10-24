"""Fixtures for tests against the **pinned, released** contract (ATLAS-001 B-19/B-20).

The contract and JSON Schemas come from ``build/specs`` (``make specs`` extracts them from atlas-specs
at the ``specs.lock`` SHA). These tests run under ``make contract`` (CI ``contract`` job); they need no
database or Redis: the app gets in-memory dependency fakes, as in the unit tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

import atlas.main
from tests.conftest import FakeDatabase, FakeRedisProbe

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "build" / "specs"
SCHEMA_FILES = {
    "HealthLive": "health-live.v1.json",
    "HealthReady": "health-ready.v1.json",
    "BuildInfo": "build-info.v1.json",
    "ErrorEnvelope": "error.v1.json",
}


@pytest.fixture(scope="session")
def contract() -> dict[str, Any]:
    path = SPECS / "contracts" / "openapi" / "atlas-api.yaml"
    if not path.exists():
        pytest.fail("pinned contract missing: run `make specs` (extracts atlas-specs at the specs.lock SHA)")
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["info"]["version"] == "0.1.0"
    return loaded


@pytest.fixture(scope="session")
def json_schemas() -> dict[str, dict[str, Any]]:
    return {
        name: json.loads((SPECS / "contracts" / "schemas" / file).read_text(encoding="utf-8"))
        for name, file in SCHEMA_FILES.items()
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ATLAS_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(atlas.main, "Database", lambda _url: FakeDatabase())
    monkeypatch.setattr(atlas.main, "create_redis", lambda _settings: FakeRedisProbe())
