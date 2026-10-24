"""B-15: real worker process round trip on Redis DB 11 with exact-key cleanup (FR-31..33, AC-10, AC-11).

A real ``taskiq worker`` subprocess (the ``make worker`` entry point) consumes this session's queue
``atlas:test:<run_id>:taskiq:queue``. The test enqueues ``atlas.diagnostics.ping``, observes the marker,
and deletes exactly the keys it caused (marker and queue). The session guards in conftest prove no
key remains under the run prefix and no key count changed elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from atlas.core.correlation import new_correlation_id
from atlas.core.ids import uuid7
from atlas.core.settings import Settings
from atlas.persistence.redis import create_redis
from atlas.workers.broker import SOCKET_TIMEOUT_SECONDS, build_broker, queue_name
from atlas.workers.middleware import CORRELATION_LABEL
from atlas.workers.producer import enqueue_ping
from atlas.workers.tasks.diagnostics import PING_TASK, PING_TTL_SECONDS, marker_name

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _worker_env(settings: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env.update(
        ATLAS_ENV_FILE=str(ROOT / "nonexistent.env"),
        ATLAS_ENV=settings.env.value,
        ATLAS_DATABASE_URL=settings.database_url,
        ATLAS_MIGRATION_DATABASE_URL=settings.migration_database_url,
        ATLAS_DB_APP_ROLE=settings.db_app_role,
        ATLAS_REDIS_URL=settings.redis_url,
        ATLAS_REDIS_KEY_PREFIX=settings.redis_key_prefix,
        ATLAS_LOG_LEVEL="INFO",
    )
    return env


class Worker:
    def __init__(self, process: subprocess.Popen[bytes], log: Path) -> None:
        self.process, self.log = process, log

    def events(self) -> list[dict[str, Any]]:
        out = []
        for line in self.log.read_text(errors="replace").splitlines():
            if line.startswith("{"):
                out.append(json.loads(line))
        return out

    def wait_for(self, predicate: Any, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events():
                if predicate(event):
                    return event
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"worker event not seen; log tail:\n{self.log.read_text(errors='replace')[-2000:]}")


@pytest.fixture(scope="module")
def worker(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worker]:
    log = tmp_path_factory.mktemp("worker") / "worker.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taskiq",
                "worker",
                "atlas.workers.entrypoint:broker",
                "--workers",
                "1",
                "--shutdown-timeout",
                "10",
                "--wait-tasks-timeout",
                "5",
            ],
            cwd=ROOT,
            env=_worker_env(settings),
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    started = Worker(process, log)
    try:
        started.wait_for(lambda e: e.get("event") == "worker_started")
        yield started
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def _produce(settings: Settings, send: Any) -> Any:
    broker, tasks = build_broker(settings)
    await broker.startup()
    try:
        return await send(tasks)
    finally:
        await broker.shutdown()


def test_worker_starts_on_the_run_queue_and_db_11(worker: Worker, settings: Settings) -> None:
    started = worker.wait_for(lambda e: e.get("event") == "worker_started")
    assert started["queue"] == queue_name(settings.redis_key_prefix)
    assert started["redis_db"] == 11
    assert started["tasks"] == [PING_TASK]


def test_ping_round_trip_carries_the_correlation_id(worker: Worker, settings: Settings, key_tracker: list[str]) -> None:
    marker, cid = f"rt_{uuid7().hex[:16]}", new_correlation_id()
    client = create_redis(settings, timeout=2.0)
    key, queue = client.key(marker_name(marker)), queue_name(settings.redis_key_prefix)
    key_tracker.extend([key, queue])

    async def run() -> tuple[str | None, int, list[str]]:
        try:
            await _produce(settings, lambda tasks: enqueue_ping(tasks, marker, cid))
            value = None
            for _ in range(150):
                value = await client.get(key)
                if value is not None:
                    break
                await asyncio.sleep(0.1)
            return value, await client.ttl(key), await client.scan_prefix()
        finally:
            await client.close()

    value, ttl, keys = asyncio.run(run())
    assert value == cid  # the worker wrote the producer's correlation ID
    assert 0 < ttl <= PING_TTL_SECONDS
    assert keys == [key]  # only the marker: the queue list is empty (consumed) and nothing else exists
    ping = worker.wait_for(lambda e: e.get("event") == "diagnostics_ping" and e.get("task_correlation_id") == cid)
    assert ping["correlation_id"] == cid  # bound from the message label in the worker's logs
    worker.wait_for(lambda e: e.get("event") == "task_succeeded" and e.get("correlation_id") == cid)


def test_invalid_message_fails_once_and_writes_nothing(
    worker: Worker, settings: Settings, key_tracker: list[str]
) -> None:
    cid = new_correlation_id()
    key_tracker.append(queue_name(settings.redis_key_prefix))

    async def send(tasks: Any) -> Any:
        return await tasks[PING_TASK].kicker().with_labels(**{CORRELATION_LABEL: cid}).kiq("bad id!", cid)

    sent = asyncio.run(_produce(settings, send))
    failed = worker.wait_for(lambda e: e.get("event") == "task_failed" and e.get("task_id") == sent.task_id)
    assert failed["exc_type"] == "InvalidTaskPayload"
    assert failed["retry"] == "none"
    time.sleep(1.0)  # give a (forbidden) retry the chance to show up
    starts = [e for e in worker.events() if e.get("event") == "task_started" and e.get("task_id") == sent.task_id]
    assert len(starts) == 1
    assert "bad id!" not in worker.log.read_text(errors="replace")  # argument values are never logged
    assert run_prefix_keys(settings) == []


def run_prefix_keys(settings: Settings) -> list[str]:
    async def scan() -> list[str]:
        client = create_redis(settings, timeout=2.0)
        try:
            return await client.scan_prefix()
        finally:
            await client.close()

    return asyncio.run(scan())


def test_idle_worker_survives_past_the_socket_timeout(worker: Worker) -> None:
    time.sleep(SOCKET_TIMEOUT_SECONDS + 1.5)  # longer than redis-py's socket read timeout
    assert worker.process.poll() is None
    log = worker.log.read_text(errors="replace")
    assert "restarted with pid" not in log
    assert "ExceptionGroup" not in log  # the crash signature of an unhandled BRPOP read timeout
    assert "Timeout reading" not in log


def test_worker_shuts_down_gracefully_on_sigterm(worker: Worker) -> None:
    worker.process.send_signal(signal.SIGTERM)
    assert worker.process.wait(timeout=20) == 0
    worker.wait_for(lambda e: e.get("event") == "worker_stopped", timeout=1)
