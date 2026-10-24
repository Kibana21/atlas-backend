"""Taskiq broker for Atlas (ATLAS-001 FR-31, B-13; DEC-098).

``ListQueueBroker`` (taskiq-redis; :class:`AtlasListQueueBroker`) on the configured Redis DB with the single queue
``<prefix>taskiq:queue``. No result backend, so no other keys are written. Tasks are registered
**explicitly** (:func:`configure`); nothing is discovered by import scanning. Building a broker
neither connects to Redis nor runs a task: connections open on ``startup()``.

Processes: the **producer** (API, scripts, tests) calls ``startup()``/``kiq()``/``shutdown()``.
The **worker** is ``taskiq worker atlas.workers.entrypoint:broker`` (``make worker``), whose
startup event creates the Atlas Redis client that tasks receive through their context.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from taskiq import AsyncBroker, AsyncTaskiqDecoratedTask, TaskiqEvents, TaskiqState
from taskiq_redis import ListQueueBroker

from atlas.core.logging import configure_logging, get_logger, install_stdlib_redaction
from atlas.core.settings import Settings, redis_db
from atlas.persistence.redis import create_redis, validate_allocation
from atlas.workers.middleware import AtlasTaskMiddleware
from atlas.workers.tasks.diagnostics import PING_TASK, PING_TIMEOUT_SECONDS, REDIS_STATE_ATTRIBUTE, ping

log = get_logger("atlas.workers")
QUEUE_SUFFIX = "taskiq:queue"
BRPOP_BLOCK_SECONDS = 1  # must stay below the socket read timeout
SOCKET_TIMEOUT_SECONDS = 5
RECONNECT_BACKOFF_SECONDS = 1.0
Tasks = dict[str, AsyncTaskiqDecoratedTask[Any, Any]]


def queue_name(prefix: str) -> str:
    return prefix + QUEUE_SUFFIX


class AtlasListQueueBroker(ListQueueBroker):
    """``ListQueueBroker`` whose consumer survives idle periods and transient Redis failures.

    Upstream ``listen()`` blocks in ``BRPOP`` forever and handles only connection errors. With
    redis-py's default socket read timeout, an idle worker then crashes every few seconds and is
    restarted by the process manager. Here ``BRPOP`` blocks for at most ``BRPOP_BLOCK_SECONDS``, and
    timeouts or disconnects are logged (type only) and retried after a short pause. The queue
    is still the single Atlas list.
    """

    async def listen(self) -> AsyncGenerator[bytes, None]:
        while True:
            try:
                async with Redis(connection_pool=self.connection_pool) as conn:
                    popped = await conn.brpop([self.queue_name], timeout=BRPOP_BLOCK_SECONDS)
            except (RedisConnectionError, RedisTimeoutError) as exc:
                log.warning("broker_listen_retry", exc_type=type(exc).__name__)
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
                continue
            if popped is not None:
                item = popped[1]
                yield item if isinstance(item, bytes) else item.encode()


def configure(broker: AsyncBroker, queue: str) -> Tasks:
    """Register Atlas middleware and tasks on ``broker``. The task list is explicit."""
    tasks: Tasks = {
        PING_TASK: broker.register_task(ping, task_name=PING_TASK, timeout=PING_TIMEOUT_SECONDS),
    }
    broker.add_middlewares(AtlasTaskMiddleware(queue, frozenset(tasks)))
    return tasks


def build_broker(settings: Settings) -> tuple[AtlasListQueueBroker, Tasks]:
    validate_allocation(settings.redis_url, settings.redis_key_prefix, redis_db(settings.redis_url))
    queue = queue_name(settings.redis_key_prefix)
    broker = AtlasListQueueBroker(
        url=settings.redis_url,
        queue_name=queue,
        max_connection_pool_size=4,
        socket_connect_timeout=2,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        client_name="atlas-taskiq",
    )
    tasks = configure(broker, queue)

    async def worker_startup(state: TaskiqState) -> None:
        configure_logging(settings.env.value, settings.log_level)
        install_stdlib_redaction()
        client = create_redis(settings, timeout=2.0)
        await client.ping()  # fail fast: a worker that cannot reach Redis must not start
        setattr(state, REDIS_STATE_ATTRIBUTE, client)
        log.info("worker_started", queue=queue, redis_db=redis_db(settings.redis_url), tasks=sorted(tasks))

    async def worker_shutdown(state: TaskiqState) -> None:
        client = getattr(state, REDIS_STATE_ATTRIBUTE, None)
        if client is not None:
            await client.close()
        log.info("worker_stopped", queue=queue)

    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, worker_startup)
    broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, worker_shutdown)
    return broker, tasks
