"""Producer helpers: enqueue Atlas tasks with validated payloads and a correlation ID."""

from __future__ import annotations

from typing import Any

from taskiq import AsyncTaskiqTask

from atlas.core.correlation import current_correlation_id
from atlas.workers.broker import Tasks
from atlas.workers.middleware import CORRELATION_LABEL
from atlas.workers.tasks.diagnostics import PING_TASK, PingPayload


async def enqueue_ping(tasks: Tasks, marker_id: str, correlation_id: str | None = None) -> AsyncTaskiqTask[Any]:
    """Send ``atlas.diagnostics.ping``; returns the sent task. The payload is validated before sending."""
    payload = PingPayload(marker_id=marker_id, correlation_id=correlation_id or current_correlation_id())
    return (
        await tasks[PING_TASK]
        .kicker()
        .with_labels(**{CORRELATION_LABEL: payload.correlation_id})
        .kiq(payload.marker_id, payload.correlation_id)
    )
