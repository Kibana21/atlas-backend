"""B-18/B-19: conformance drills against the pinned contract-v0.1.0 (AC-03 cases b, c, e, f).

Each drill changes the real application (or its exported schema) the way a mistaken code change
would, runs the pinned atlas-specs conformance tool, and requires it to fail with the documented
code. The unchanged application must conform. This keeps the CI ``contract`` job honest: it cannot
pass a schema that differs from the released contract.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import APIRouter
from scripts.contract_subset import subset
from scripts.export_openapi import EXPORT_SETTINGS, export

from atlas.api.routers import health
from atlas.main import create_app
from tests.contract.conftest import ROOT, SPECS

pytestmark = [pytest.mark.contract, pytest.mark.pinned_specs]
TOOL = SPECS / "tools" / "contract_conformance.py"


def _conform(tmp_path: Path, implementation: dict[str, Any], pending: list[dict[str, Any]] | None = None) -> str:
    contract = yaml.safe_load((SPECS / "contracts" / "openapi" / "atlas-api.yaml").read_text(encoding="utf-8"))
    compared = tmp_path / "contract.json"
    compared.write_text(json.dumps(subset(contract, pending or [])), encoding="utf-8")
    impl = tmp_path / "implementation.json"
    impl.write_text(json.dumps(implementation, sort_keys=True), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv: the pinned atlas-specs tool
        [  # noqa: S607 - uv resolved from PATH, as in the Makefile
            "uv",
            "run",
            "--quiet",
            "--locked",
            "--project",
            str(SPECS / "tools"),
            "python",
            str(TOOL),
            "conform",
            "--contract",
            str(compared),
            "--implementation",
            str(impl),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    output = result.stdout + result.stderr
    return ("PASS\n" if result.returncode == 0 else "FAIL\n") + output


def _app_schema(change: Callable[[Any], None] | None = None) -> dict[str, Any]:
    from atlas.core.settings import Settings

    app = create_app(Settings(_env_file=None, **EXPORT_SETTINGS))  # type: ignore[arg-type]
    if change is not None:
        change(app)
        app.openapi_schema = None
    return dict(app.openapi())


def test_unchanged_application_conforms_and_export_is_deterministic(tmp_path: Path) -> None:
    assert export() == export()
    outcome = _conform(tmp_path, json.loads(export()))
    assert outcome.startswith("PASS"), outcome


def test_drill_b_missing_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # As if the route were deleted from the health router's source (FastAPI nests included routers).
    kept = [r for r in health.router.routes if getattr(r, "path", "") != "/health/live"]
    assert len(kept) == len(health.router.routes) - 1
    monkeypatch.setattr(health.router, "routes", kept)
    outcome = _conform(tmp_path, _app_schema())
    assert outcome.startswith("FAIL"), outcome
    assert "MISSING_OPERATION" in outcome, outcome
    assert "getHealthLive" in outcome, outcome


def test_drill_c_undeclared_operation(tmp_path: Path) -> None:
    def add_extra(app: Any) -> None:
        router = APIRouter(prefix="/api/v1")

        @router.get("/diagnostics/extra", operation_id="getDiagnosticsExtra")
        def extra() -> dict[str, str]:
            return {"x": "y"}

        app.include_router(router)

    outcome = _conform(tmp_path, _app_schema(add_extra))
    assert outcome.startswith("FAIL"), outcome
    assert "UNEXPECTED_OPERATION" in outcome, outcome
    assert "/api/v1/diagnostics/extra" in outcome, outcome


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "e: extra optional response property",
            lambda s: s["components"]["schemas"]["HealthLive"]["properties"].update(uptime={"type": "integer"}),
        ),
        (
            "rename a response field",
            lambda s: s["components"]["schemas"]["HealthLive"]["properties"].update(
                state=s["components"]["schemas"]["HealthLive"]["properties"].pop("status")
            ),
        ),
        (
            "f: required property removed",
            lambda s: s["components"]["schemas"]["ReadinessChecks"]["required"].remove("redis"),
        ),
        (
            "f: status code changed",
            lambda s: s["paths"]["/api/v1/health/ready"]["get"]["responses"].update(
                {"502": s["paths"]["/api/v1/health/ready"]["get"]["responses"].pop("503")}
            ),
        ),
        ("operationId changed", lambda s: s["paths"]["/api/v1/health/live"]["get"].update(operationId="getLive")),
    ],
)
def test_drill_schema_changes_fail(tmp_path: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    schema = json.loads(export())
    mutate(schema)
    outcome = _conform(tmp_path, schema)
    assert outcome.startswith("FAIL"), f"{name}: {outcome}"


def test_drill_re_adding_a_pending_entry_fails(tmp_path: Path) -> None:
    entry = {
        "operation_id": "getHealthReady",
        "method": "GET",
        "path": "/api/v1/health/ready",
        "lands_in": "drill",
        "components": ["HealthReady", "ReadinessChecks"],
    }
    outcome = _conform(tmp_path, json.loads(export()), pending=[entry])
    assert outcome.startswith("FAIL"), outcome
    assert "UNEXPECTED_OPERATION" in outcome, outcome
    assert "getHealthReady" in outcome, outcome
