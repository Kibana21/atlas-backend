"""Baseline API security headers (ATLAS-001 FR-38)."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_RAW = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
_NAMES = {k for k, _ in _RAW}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k not in _NAMES] + _RAW
            await send(message)

        await self.app(scope, receive, send_with_headers)
