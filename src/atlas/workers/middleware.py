"""Taskiq middleware for every Atlas broker (ATLAS-001 FR-23/FR-25/FR-31).

* **Allowlist:** only explicitly registered task names are sent or executed.
* **Queue isolation:** taskiq-redis sends to ``labels["queue_name"]`` when present, which could
  target another application's list. Any override that is not the Atlas queue is refused.
* **Correlation:** the producer stamps a ``correlation_id`` label (the current request's ID, or a
  fresh one). The worker binds it to the context variable and structured logs for the task.
* **Failure reporting:** each failure is logged once (``task_failed``, task, ID, exception type;
  never arguments or messages), and the result is an error. Nothing is retried automatically
  (DEC-046 → BL-201).
"""

from __future__ import annotations

from typing import Any

import structlog
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from atlas.core.correlation import VALID, accept_or_generate, correlation_id_var, current_correlation_id
from atlas.core.logging import get_logger
from atlas.persistence.redis import RedisIsolationError

log = get_logger("atlas.workers")
CORRELATION_LABEL = "correlation_id"
QUEUE_LABEL = "queue_name"


class UnregisteredTaskError(LookupError):
    """A message names a task that Atlas did not register explicitly."""


class AtlasTaskMiddleware(TaskiqMiddleware):
    def __init__(self, queue_name: str, task_names: frozenset[str]) -> None:
        super().__init__()
        self.queue_name = queue_name
        self.task_names = task_names
        self._tokens: dict[str, Any] = {}

    def _check(self, message: TaskiqMessage) -> None:
        if message.task_name not in self.task_names:
            raise UnregisteredTaskError(f"task {message.task_name!r} is not registered")
        queue = message.labels.get(QUEUE_LABEL)
        if queue is not None and queue != self.queue_name:
            raise RedisIsolationError("tasks may only use the Atlas queue")

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        self._check(message)
        label = message.labels.get(CORRELATION_LABEL)
        cid = str(label) if label is not None and VALID.fullmatch(str(label)) else current_correlation_id()
        message.labels[CORRELATION_LABEL] = cid
        log.info("task_enqueued", task=message.task_name, task_id=message.task_id, correlation_id=cid)
        return message

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        self._check(message)
        label = message.labels.get(CORRELATION_LABEL)
        cid = accept_or_generate(str(label) if label is not None else None)
        message.labels[CORRELATION_LABEL] = cid
        self._tokens[message.task_id] = correlation_id_var.set(cid)
        structlog.contextvars.bind_contextvars(correlation_id=cid, task=message.task_name, task_id=message.task_id)
        log.info("task_started")
        return message

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        try:
            if result.is_err:
                log.error(
                    "task_failed",
                    exc_type=type(result.error).__name__,
                    duration_s=round(result.execution_time, 3),
                    retry="none",
                )
            else:
                log.info("task_succeeded", duration_s=round(result.execution_time, 3))
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id", "task", "task_id")
            token = self._tokens.pop(message.task_id, None)
            if token is not None:
                correlation_id_var.reset(token)
