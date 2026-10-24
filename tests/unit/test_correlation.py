"""Correlation IDs (FR-23, AC-10)."""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from atlas.core.correlation import CorrelationIdMiddleware, accept_or_generate, current_correlation_id
from atlas.core.ids import uuid7

pytestmark = pytest.mark.unit


async def echo(_: Request) -> JSONResponse:
    return JSONResponse({"seen": current_correlation_id()}, headers={"X-Correlation-ID": "spoofed-by-handler"})


def _client() -> TestClient:
    app = Starlette(routes=[Route("/echo", echo)])
    app.add_middleware(CorrelationIdMiddleware)
    return TestClient(app)


def test_valid_inbound_id_is_echoed() -> None:
    r = _client().get("/echo", headers={"X-Correlation-ID": "req_test123456"})
    assert r.headers["X-Correlation-ID"] == "req_test123456"
    assert r.json() == {"seen": "req_test123456"}
    assert r.headers.get_list("X-Correlation-ID") == ["req_test123456"]  # exactly one header


@pytest.mark.parametrize("bad", ["<script>alert(1)</script>", "short", "x" * 65, "has space here", "dots.not.allowed"])
def test_invalid_inbound_id_is_replaced_never_echoed(bad: str) -> None:
    r = _client().get("/echo", headers={"X-Correlation-ID": bad})
    cid = r.headers["X-Correlation-ID"]
    assert cid != bad
    assert cid.startswith("req_")
    assert len(cid) == 36
    assert r.json()["seen"] == cid


def test_missing_inbound_id_generates_one() -> None:
    a, b = (_client().get("/echo").headers["X-Correlation-ID"] for _ in range(2))
    assert a != b
    assert a.startswith("req_")


def test_accept_or_generate() -> None:
    assert accept_or_generate("abcdefgh") == "abcdefgh"
    assert accept_or_generate(None).startswith("req_")


def test_uuid7_layout_and_ordering() -> None:
    ids = [uuid7() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(u.version == 7 and u.variant == uuid.RFC_4122 for u in ids)
    stamps = [u.int >> 80 for u in ids]
    assert stamps == sorted(stamps)  # millisecond timestamps never go backwards
