"""Uniform error envelope (Tech §6.3, contracts/conventions.md §6-7, ATLAS-001 FR-24).

Every non-2xx response is an ErrorEnvelope whose correlation_id equals the X-Correlation-ID
header. Messages never contain stack traces, SQL, paths, host names or settings values; the
unhandled-error path logs the exception (redacted) and returns a generic message.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atlas.api.schemas.common import ErrorBody, ErrorEnvelope
from atlas.core.correlation import HEADER, current_correlation_id
from atlas.core.logging import get_logger

log = get_logger("atlas.errors")


class AtlasError(Exception):
    code = "INTERNAL_ERROR"
    status_code = 500
    recoverable = False
    default_message = "An unexpected error occurred."

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or {}


class NotFoundError(AtlasError):
    code, status_code, default_message = "NOT_FOUND", 404, "The requested resource was not found."


class ValidationFailedError(AtlasError):
    code, status_code, default_message = "VALIDATION_FAILED", 422, "The request is invalid."


class DependencyUnavailableError(AtlasError):
    code, status_code, recoverable = "DEPENDENCY_UNAVAILABLE", 503, True
    default_message = "A required dependency is unavailable."


def error_response(error: AtlasError) -> JSONResponse:
    cid = current_correlation_id()
    body = ErrorEnvelope(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            correlation_id=cid,
            details=error.details,
            recoverable=error.recoverable,
        )
    )
    return JSONResponse(body.model_dump(), status_code=error.status_code, headers={HEADER: cid})


async def _atlas_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AtlasError)  # noqa: S101 - registered for AtlasError only
    return error_response(exc)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for RequestValidationError only
    # Location, message and type only: never echo the submitted input back.
    errors = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in exc.errors()
    ]
    return error_response(ValidationFailedError(details={"errors": errors}))


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for HTTPException only
    if exc.status_code in (404, 405):
        # Unknown routes and unsupported methods are indistinguishable from missing resources
        # (anti-enumeration, conventions §5); no code outside the append-only catalog is invented.
        return error_response(NotFoundError())
    if 400 <= exc.status_code < 500:
        return error_response(ValidationFailedError())
    return error_response(AtlasError())


class UnhandledErrorMiddleware:
    """Turn unhandled exceptions into the INTERNAL_ERROR envelope.

    Installed *inside* the correlation and security-header middleware, so the 500 response keeps
    the request's correlation ID and baseline headers. Starlette's own ServerErrorMiddleware runs
    outermost, after those middlewares have exited, and would lose both.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            log.error("unhandled_exception", exc_type=type(exc).__name__, exc_info=exc)
            if started:
                raise  # response already partly sent; nothing safe to replace it with
            await error_response(AtlasError())(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AtlasError, _atlas_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
