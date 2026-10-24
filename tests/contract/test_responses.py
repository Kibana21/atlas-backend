"""B-20: real responses of every released operation validate against contract-v0.1.0 (AC-08, FR-24, FR-26).

Each scenario calls the real app (dependency fakes only) and checks, against the pinned contract:
declared status, ``application/json``, the ``X-Correlation-ID`` header (pattern, and equal to the
body's ``correlation_id`` on errors), the response schema (required fields, types, constants) with
no undeclared fields, and the matching JSON Schema file. Meta-tests prove that the validator rejects
deliberately broken responses, so a contract violation cannot pass silently.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import Query
from fastapi.testclient import TestClient

from atlas.main import create_app
from tests.conftest import FakeDatabase, FakeRedisProbe, make_settings
from tests.contract.validation import ContractViolationError, operation, validate_json_schema, validate_response

pytestmark = [pytest.mark.contract, pytest.mark.pinned_specs]
SHA = "0123456789abcdef0123456789abcdef01234567"
FORBIDDEN_IN_ERRORS = ("Traceback", "localhost", "postgresql", "redis://", "5432", "6379", "atlas_test_app", "SELECT")

Scenario = tuple[str, str, Callable[[], Any], int, str]
SCENARIOS: list[Scenario] = [
    ("live", "/api/v1/health/live", lambda: create_app(make_settings()), 200, "HealthLive"),
    (
        "ready",
        "/api/v1/health/ready",
        lambda: create_app(make_settings(), database=FakeDatabase(), redis=FakeRedisProbe()),
        200,
        "HealthReady",
    ),
    (
        "ready-database-down",
        "/api/v1/health/ready",
        lambda: create_app(make_settings(), database=FakeDatabase(available=False), redis=FakeRedisProbe()),
        503,
        "ErrorEnvelope",
    ),
    (
        "ready-redis-down",
        "/api/v1/health/ready",
        lambda: create_app(make_settings(), database=FakeDatabase(), redis=FakeRedisProbe(available=False)),
        503,
        "ErrorEnvelope",
    ),
    (
        "build",
        "/api/v1/diagnostics/build",
        lambda: create_app(make_settings(build_sha=SHA), database=FakeDatabase(revision="0001_baseline")),
        200,
        "BuildInfo",
    ),
    (
        "build-disabled",
        "/api/v1/diagnostics/build",
        lambda: create_app(make_settings(diagnostics_enabled=False)),
        404,
        "ErrorEnvelope",
    ),
]


def _call(app: Any, path: str, cid: str | None = None) -> Any:
    headers = {"X-Correlation-ID": cid} if cid else {}
    return TestClient(app, raise_server_exceptions=False).get(path, headers=headers)


@pytest.mark.parametrize(("name", "path", "factory", "status", "component"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_operation_response_conforms(
    contract: dict[str, Any],
    json_schemas: dict[str, dict[str, Any]],
    name: str,
    path: str,
    factory: Callable[[], Any],
    status: int,
    component: str,
) -> None:
    r = _call(factory(), path, cid="contract_cid_01")
    assert r.status_code == status, name
    body = r.json()
    assert validate_response(contract, "GET", path, r.status_code, r.headers, body) == component
    validate_json_schema(json_schemas[component], body)
    assert r.headers["x-correlation-id"] == "contract_cid_01"  # a valid inbound ID is echoed
    if component == "ErrorEnvelope":
        assert body["error"]["correlation_id"] == r.headers["x-correlation-id"]
        assert not [f for f in FORBIDDEN_IN_ERRORS if f in r.text]


def test_readiness_failure_names_the_check(contract: dict[str, Any]) -> None:
    db_down = _call(SCENARIOS[2][2](), "/api/v1/health/ready").json()["error"]
    redis_down = _call(SCENARIOS[3][2](), "/api/v1/health/ready").json()["error"]
    assert (db_down["code"], db_down["details"]) == ("DEPENDENCY_UNAVAILABLE", {"failed_check": "database"})
    assert (redis_down["code"], redis_down["details"]) == ("DEPENDENCY_UNAVAILABLE", {"failed_check": "redis"})
    assert db_down["recoverable"] is redis_down["recoverable"] is True


def test_generated_correlation_ids_match_the_contract_pattern(contract: dict[str, Any]) -> None:
    for cid in (None, "bad id!", "x" * 65):  # absent or invalid inbound IDs are replaced
        r = _call(create_app(make_settings()), "/api/v1/health/live", cid=cid)
        validate_response(contract, "GET", "/api/v1/health/live", r.status_code, r.headers, r.json())
        assert r.headers["x-correlation-id"] != cid


def test_every_released_operation_is_covered(contract: dict[str, Any]) -> None:
    released = {(p, m) for p, item in contract["paths"].items() for m in item}
    covered = {(path, "get") for _, path, _, _, _ in SCENARIOS}
    assert released == covered
    for path, method in released:
        declared = {int(s) for s in operation(contract, method, path)["responses"]}
        exercised = {status for _, p, _, status, _ in SCENARIOS if p == path}
        assert exercised <= declared
    # 500 is declared everywhere and is exercised below through the shared error middleware.


def _app_with_test_routes() -> Any:
    """The real app plus two test-only routes (never in the schema) to provoke 422 and 500."""
    app = create_app(make_settings())

    @app.get("/api/v1/_test/validation", include_in_schema=False)
    def needs_int(n: int = Query()) -> dict[str, int]:
        return {"n": n}

    @app.get("/api/v1/_test/boom", include_in_schema=False)
    def boom() -> None:
        raise RuntimeError("SELECT * FROM secret_table at localhost:5432 password=hunter2")

    return app


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/v1/_test/validation?n=notanint", 422, "VALIDATION_FAILED"),
        ("/api/v1/_test/boom", 500, "INTERNAL_ERROR"),
        ("/api/v1/does-not-exist", 404, "NOT_FOUND"),
    ],
)
def test_error_responses_use_the_released_envelope(
    contract: dict[str, Any], json_schemas: dict[str, dict[str, Any]], path: str, status: int, code: str
) -> None:
    r = _call(_app_with_test_routes(), path)
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    validate_json_schema(json_schemas["ErrorEnvelope"], body)
    # 500 is declared on every operation: validate the 500 body exactly as the contract declares it.
    validate_response(contract, "GET", "/api/v1/health/live", 500, r.headers, body)
    assert body["error"]["code"] == code
    assert body["error"]["correlation_id"] == r.headers["x-correlation-id"]
    assert "hunter2" not in r.text
    assert "notanint" not in r.text  # submitted input is never echoed
    assert not [f for f in FORBIDDEN_IN_ERRORS if f in r.text]


# ---- meta-tests: a deliberately broken response must fail validation ------------------------------


def _ok(contract: dict[str, Any], path: str, factory: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    r = _call(factory(), path)
    return r.headers, r.json()


MUTATIONS: list[tuple[str, int, Callable[[dict[str, Any]], object], str]] = [
    ("/api/v1/health/live", 200, lambda b: b.update(extra="x"), "undeclared properties"),
    ("/api/v1/health/live", 200, lambda b: b.update(status="degraded"), "'ok' was expected"),
    ("/api/v1/health/ready", 200, lambda b: b["checks"].pop("redis"), "'redis' is a required property"),
    ("/api/v1/health/ready", 200, lambda b: b["checks"].update(queue="ok"), "undeclared properties"),
    ("/api/v1/diagnostics/build", 200, lambda b: b.update(backend_sha="main"), "does not match"),
    ("/api/v1/diagnostics/build", 200, lambda b: b.pop("db_migration_revision"), "required property"),
]


@pytest.mark.parametrize(("path", "status", "mutate", "message"), MUTATIONS)
def test_deliberate_body_violations_are_rejected(
    contract: dict[str, Any], path: str, status: int, mutate: Callable[[dict[str, Any]], object], message: str
) -> None:
    factory = next(f for _, p, f, s, _ in SCENARIOS if p == path and s == status)
    headers, body = _ok(contract, path, factory)
    validate_response(contract, "GET", path, status, headers, body)  # the real response passes
    broken = copy.deepcopy(body)
    mutate(broken)
    with pytest.raises(ContractViolationError, match=message):
        validate_response(contract, "GET", path, status, headers, broken)


def test_deliberate_status_header_and_media_violations_are_rejected(contract: dict[str, Any]) -> None:
    headers, body = _ok(contract, "/api/v1/health/live", SCENARIOS[0][2])
    with pytest.raises(ContractViolationError, match="status 201 not declared"):
        validate_response(contract, "GET", "/api/v1/health/live", 201, headers, body)
    with pytest.raises(ContractViolationError, match="missing header X-Correlation-ID"):
        validate_response(contract, "GET", "/api/v1/health/live", 200, {"content-type": "application/json"}, body)
    bad_header = {"content-type": "application/json", "X-Correlation-ID": "no!"}
    with pytest.raises(ContractViolationError, match="violates"):
        validate_response(contract, "GET", "/api/v1/health/live", 200, bad_header, body)
    text = {"content-type": "text/plain", "X-Correlation-ID": "abcdefgh1"}
    with pytest.raises(ContractViolationError, match="content type"):
        validate_response(contract, "GET", "/api/v1/health/live", 200, text, body)
    with pytest.raises(ContractViolationError, match="not a contract operation"):
        validate_response(contract, "GET", "/api/v1/diagnostics/extra", 200, headers, body)
    envelope = json.loads(json.dumps({"error": {"code": "NOT_FOUND", "message": "m", "correlation_id": "abcdefgh1"}}))
    with pytest.raises(ContractViolationError, match="required property"):
        validate_response(contract, "GET", "/api/v1/diagnostics/build", 404, headers, envelope)
