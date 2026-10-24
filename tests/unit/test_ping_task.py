"""B-14: diagnostics.ping task body against a fake client: key name, TTL, log field (FR-32)."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from atlas.core.logging import configure_logging
from atlas.workers.tasks.diagnostics import PING_TTL_SECONDS, InvalidTaskPayload, PingPayload, ping

pytestmark = pytest.mark.unit
PREFIX = "atlas:test:unit:"


class _Recorder:
    prefix = PREFIX

    def __init__(self) -> None:
        self.sets: list[tuple[str, str, int]] = []

    def key(self, name: str) -> str:
        return PREFIX + name

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        self.sets.append((key, value, ttl_seconds))


def _context(redis: _Recorder) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(atlas_redis=redis))


@pytest.mark.anyio
async def test_ping_writes_the_prefixed_marker_with_a_short_ttl_and_logs_the_correlation_id() -> None:
    stream = io.StringIO()
    configure_logging("test", stream=stream)
    redis = _Recorder()
    await ping("marker_0001", "req_abcdef12", _context(redis))  # type: ignore[arg-type]
    assert redis.sets == [(PREFIX + "ping:marker_0001", "req_abcdef12", PING_TTL_SECONDS)]
    assert 0 < PING_TTL_SECONDS <= 60
    event = json.loads(stream.getvalue().splitlines()[-1])
    assert event["event"] == "diagnostics_ping"
    assert event["task_correlation_id"] == "req_abcdef12"


@pytest.mark.anyio
async def test_ping_is_idempotent() -> None:
    redis = _Recorder()
    for _ in range(2):  # a redelivered message rewrites the same key and value
        await ping("marker_0001", "req_abcdef12", _context(redis))  # type: ignore[arg-type]
    assert len(set(redis.sets)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("marker", "cid"), [("short", "req_abcdef12"), ("marker_0001", "bad id!"), ("a" * 65, "x" * 8)]
)
async def test_invalid_payload_is_rejected_before_any_write(marker: str, cid: str) -> None:
    redis = _Recorder()
    with pytest.raises(InvalidTaskPayload) as caught:
        await ping(marker, cid, _context(redis))  # type: ignore[arg-type]
    assert redis.sets == []
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__  # the pydantic error (which echoes inputs) is not chained
    assert marker not in str(caught.value)
    assert cid not in str(caught.value)


def test_payload_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PingPayload.model_validate({"marker_id": "marker_0001", "correlation_id": "req_abcdef12", "x": 1})
