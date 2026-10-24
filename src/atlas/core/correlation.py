"""Correlation IDs (ATLAS-001 FR-23, contracts/conventions.md §3).

A valid inbound ``X-Correlation-ID`` is reused; anything else is replaced by ``req_<uuid7 hex>``.
The ID is stored in a context variable, bound into structured logs, and set on every response.
Implemented as pure ASGI middleware so that it also wraps streaming and error responses.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atlas.core.ids import uuid7

HEADER = "X-Correlation-ID"
_HEADER_BYTES = HEADER.lower().encode()
VALID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return f"req_{uuid7().hex}"


def accept_or_generate(inbound: str | None) -> str:
    if inbound is not None and VALID.fullmatch(inbound):
        return inbound
    return new_correlation_id()


def current_correlation_id() -> str:
    """The request's correlation ID, or a fresh one outside a request."""
    return correlation_id_var.get() or new_correlation_id()


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = next((v.decode("latin-1") for k, v in scope["headers"] if k == _HEADER_BYTES), None)
        cid = accept_or_generate(inbound)
        token = correlation_id_var.set(cid)
        structlog.contextvars.bind_contextvars(correlation_id=cid)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != _HEADER_BYTES]
                headers.append((_HEADER_BYTES, cid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id")
            correlation_id_var.reset(token)
