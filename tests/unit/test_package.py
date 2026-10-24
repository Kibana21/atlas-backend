"""Sanity check: the package and its layout (Tech §17.1, FR-20) import."""

import importlib

import pytest

MODULES = [
    "atlas", "atlas.api", "atlas.core", "atlas.domain", "atlas.connectors", "atlas.workers",
    "atlas.persistence", "atlas.modules.catalog", "atlas.modules.intelligence",
]


@pytest.mark.unit
@pytest.mark.parametrize("name", MODULES)
def test_package_layout_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
