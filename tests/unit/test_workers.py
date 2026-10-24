"""Taskiq seam (FR-31/FR-32, B-13/B-14) on taskiq's in-process broker: real middleware and tasks."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from taskiq import InMemoryBroker

from atlas.core.correlation import correlation_id_var
from atlas.core.logging import configure_logging
from atlas.persistence.redis import RedisIsolationError, RedisUnavailable
from atlas.workers import broker as broker_module
from atlas.workers.broker import Tasks, build_broker, configure, queue_name
from atlas.workers.middleware import UnregisteredTaskError
from atlas.workers.producer import enqueue_ping
from atlas.workers.tasks.diagnostics import PING_TASK
from tests.conftest import make_settings

pytestmark = pytest.mark.unit
PREFIX = "atlas:test:unit:"


class _Redis:
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail, self.delay = fail, delay
        self.sets: list[tuple[str, str, int]] = []

    def key(self, name: str) -> str:
        return PREFIX + name

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        self.sets.append((key, value, ttl_seconds))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RedisUnavailable


class _Harness:
    def __init__(self, broker: InMemoryBroker, tasks: Tasks, redis: _Redis, logs: io.StringIO) -> None:
        self.broker, self.tasks, self.redis, self.logs = broker, tasks, redis, logs

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.logs.getvalue().splitlines()]


@pytest.fixture
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[_Harness]:
    logs = io.StringIO()
    configure_logging("test", stream=logs)
    broker = InMemoryBroker()
    tasks = configure(broker, queue_name(PREFIX))
    redis = getattr(request, "param", None) or _Redis()
    broker.state.atlas_redis = redis
    await broker.startup()
    try:
        yield _Harness(broker, tasks, redis, logs)
    finally:
        await broker.shutdown()


@pytest.mark.anyio
async def test_correlation_id_crosses_the_producer_worker_boundary(harness: _Harness) -> None:
    token = correlation_id_var.set("req_parent0001")
    try:
        sent = await enqueue_ping(harness.tasks, "marker_0001")
    finally:
        correlation_id_var.reset(token)
    assert not (await sent.wait_result(timeout=2)).is_err
    assert harness.redis.sets == [(PREFIX + "ping:marker_0001", "req_parent0001", 60)]
    by_event = {e["event"]: e for e in harness.events()}
    assert by_event["task_enqueued"]["correlation_id"] == "req_parent0001"
    for event in ("task_started", "diagnostics_ping", "task_succeeded"):
        assert by_event[event]["correlation_id"] == "req_parent0001"
        assert by_event[event]["task"] == PING_TASK
    assert correlation_id_var.get() is None  # worker context does not leak


@pytest.mark.anyio
@pytest.mark.parametrize("harness", [_Redis(fail=True)], indirect=True)
async def test_failure_is_reported_once_and_not_retried(harness: _Harness) -> None:
    sent = (
        await harness.tasks[PING_TASK].kicker().with_labels(correlation_id="req_failure01").kiq("marker_0002", "x" * 8)
    )
    result = await sent.wait_result(timeout=2)
    assert result.is_err
    assert len(harness.redis.sets) == 1  # executed exactly once: no automatic retry (DEC-046)
    failures = [e for e in harness.events() if e["event"] == "task_failed"]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "RedisUnavailable"
    assert failures[0]["retry"] == "none"
    assert failures[0]["correlation_id"] == "req_failure01"


@pytest.mark.anyio
@pytest.mark.parametrize("harness", [_Redis(delay=1)], indirect=True)
async def test_task_timeout_is_a_reported_failure(harness: _Harness) -> None:
    sent = await harness.tasks[PING_TASK].kicker().with_labels(timeout=0.05).kiq("marker_0003", "req_timeout1")
    result = await sent.wait_result(timeout=3)
    assert result.is_err
    assert [e["event"] for e in harness.events()].count("task_failed") == 1


@pytest.mark.anyio
async def test_invalid_payload_is_rejected_by_the_producer(harness: _Harness) -> None:
    with pytest.raises(ValueError, match="marker_id"):
        await enqueue_ping(harness.tasks, "bad id!", "req_abcdef12")
    assert harness.redis.sets == []


@pytest.mark.anyio
async def test_invalid_payload_sent_anyway_fails_in_the_worker_without_writing(harness: _Harness) -> None:
    sent = await harness.tasks[PING_TASK].kiq("bad id!", "req_abcdef12")
    assert (await sent.wait_result(timeout=2)).is_err
    assert harness.redis.sets == []


@pytest.mark.anyio
async def test_queue_override_to_another_list_is_refused(harness: _Harness) -> None:
    with pytest.raises(RedisIsolationError, match="Atlas queue"):
        await (
            harness.tasks[PING_TASK]
            .kicker()
            .with_labels(queue_name="other-app:queue")
            .kiq("marker_0004", "req_q0000001")
        )
    assert harness.redis.sets == []


@pytest.mark.anyio
async def test_only_explicitly_registered_tasks_are_sent(harness: _Harness) -> None:
    async def rogue() -> None:  # registered after configure(): not on the Atlas allowlist
        raise AssertionError("must never run")

    task = harness.broker.register_task(rogue, task_name="other.rogue")
    with pytest.raises(UnregisteredTaskError):
        await task.kiq()


def test_build_broker_uses_the_atlas_queue_and_db_without_connecting() -> None:
    broker, tasks = build_broker(make_settings())
    assert broker.queue_name == "atlas:test:unit:taskiq:queue"
    assert list(tasks) == [PING_TASK]
    assert broker.connection_pool.connection_kwargs["db"] == 11
    assert broker.result_backend.__class__.__name__ == "DummyResultBackend"  # writes no result keys


def test_importing_the_worker_package_builds_no_broker() -> None:
    assert not any(
        isinstance(v, InMemoryBroker | type(build_broker(make_settings())[0])) for v in vars(broker_module).values()
    )


class _FakeConn:
    def __init__(self, script: list[Any], calls: list[tuple[Any, ...]]) -> None:
        self.script, self.calls = script, calls

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def brpop(self, keys: list[str], timeout: int) -> Any:
        self.calls.append((tuple(keys), timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.mark.anyio
async def test_listen_blocks_briefly_and_survives_timeouts_and_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    logs = io.StringIO()
    configure_logging("test", stream=logs)
    calls: list[tuple[Any, ...]] = []
    script: list[Any] = [
        None,
        RedisTimeoutError("Timeout reading"),
        RedisConnectionError("gone"),
        ("q", b"m1"),
        ("q", "m2"),
    ]
    monkeypatch.setattr(broker_module, "Redis", lambda connection_pool: _FakeConn(script, calls))
    monkeypatch.setattr(broker_module, "RECONNECT_BACKOFF_SECONDS", 0)
    broker, _ = build_broker(make_settings())
    received = []
    async for message in broker.listen():
        received.append(message)
        if len(received) == 2:
            break
    assert received == [b"m1", b"m2"]
    assert set(calls) == {(("atlas:test:unit:taskiq:queue",), broker_module.BRPOP_BLOCK_SECONDS)}
    assert broker_module.BRPOP_BLOCK_SECONDS < broker_module.SOCKET_TIMEOUT_SECONDS
    retries = [json.loads(line) for line in logs.getvalue().splitlines() if "broker_listen_retry" in line]
    assert [r["exc_type"] for r in retries] == ["TimeoutError", "ConnectionError"]
    assert "Timeout reading" not in logs.getvalue()
