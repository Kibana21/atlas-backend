"""Error envelope (FR-24, AC-08, AC-12)."""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from atlas.core.correlation import CorrelationIdMiddleware
from atlas.core.errors import (
    DependencyUnavailableError,
    NotFoundError,
    UnhandledErrorMiddleware,
    register_error_handlers,
)
from atlas.core.logging import configure_logging
from atlas.core.security_headers import SecurityHeadersMiddleware

pytestmark = pytest.mark.unit

LEAKY = "SELECT password FROM users at /srv/atlas/app.py on db.internal.example port 5432 secret=hunter2"


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    # Same order as create_app: correlation (outermost) -> security headers -> unhandled errors.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/missing")
    def missing() -> None:
        raise NotFoundError()

    @app.get("/dependency")
    def dependency() -> None:
        raise DependencyUnavailableError(details={"failed_check": "redis"})

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(LEAKY)

    @app.get("/typed")
    def typed(n: int = Query()) -> dict[str, int]:
        return {"n": n}

    return app


def _assert_envelope(resp: object, status: int, code: str, recoverable: bool) -> dict[str, object]:
    assert getattr(resp, "status_code") == status  # noqa: B009
    body: dict[str, object] = getattr(resp, "json")()  # noqa: B009
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "correlation_id", "details", "recoverable"}
    assert error["code"] == code
    assert error["recoverable"] is recoverable
    assert error["correlation_id"] == getattr(resp, "headers")["X-Correlation-ID"]  # noqa: B009
    return error


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_atlas_errors(client: TestClient) -> None:
    _assert_envelope(client.get("/missing"), 404, "NOT_FOUND", False)
    err = _assert_envelope(client.get("/dependency"), 503, "DEPENDENCY_UNAVAILABLE", True)
    assert err["details"] == {"failed_check": "redis"}


def test_unknown_route_and_wrong_method_are_not_found(client: TestClient) -> None:
    _assert_envelope(client.get("/no-such-route"), 404, "NOT_FOUND", False)
    _assert_envelope(client.post("/missing"), 404, "NOT_FOUND", False)


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    resp = client.get("/typed", params={"n": "not-a-number-<script>"})
    err = _assert_envelope(resp, 422, "VALIDATION_FAILED", False)
    assert "<script>" not in resp.text
    details = err["details"]
    assert isinstance(details, dict)
    assert details["errors"][0]["loc"] == ["query", "n"]


def test_unhandled_error_is_generic_and_logged_redacted(client: TestClient) -> None:
    stream = io.StringIO()
    configure_logging("test", stream=stream)
    resp = client.get("/boom", headers={"X-Correlation-ID": "req_test123456"})
    err = _assert_envelope(resp, 500, "INTERNAL_ERROR", False)
    assert err["correlation_id"] == "req_test123456"
    assert err["message"] == "An unexpected error occurred."
    for fragment in ("SELECT", "Traceback", "/srv/atlas", "db.internal", "hunter2", "RuntimeError", "5432"):
        assert fragment not in resp.text
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["event"] == "unhandled_exception"
    assert record["correlation_id"] == "req_test123456"
    assert "hunter2" not in stream.getvalue()


def test_other_http_exceptions_map_to_catalog_codes() -> None:
    from fastapi import HTTPException

    app = _app()

    @app.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=418)

    @app.get("/gateway")
    def gateway() -> None:
        raise HTTPException(status_code=502)

    client = TestClient(app, raise_server_exceptions=False)
    _assert_envelope(client.get("/teapot"), 422, "VALIDATION_FAILED", False)
    _assert_envelope(client.get("/gateway"), 500, "INTERNAL_ERROR", False)


@pytest.mark.anyio
async def test_unhandled_middleware_passes_non_http_scopes_and_reraises_after_start() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    await UnhandledErrorMiddleware(lifespan_app)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]

    async def half_sent(scope: object, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        raise RuntimeError("after start")

    sent: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        sent.append(message)

    with pytest.raises(RuntimeError, match="after start"):
        await UnhandledErrorMiddleware(half_sent)({"type": "http"}, None, capture)  # type: ignore[arg-type]
    assert [m["type"] for m in sent] == ["http.response.start"]  # no second, conflicting response
