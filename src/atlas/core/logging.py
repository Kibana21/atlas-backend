"""Structured JSON logging with redaction (ATLAS-001 FR-25, NFR-08).

Atlas-only operational logs: no telemetry SDKs, no enterprise telemetry ingestion (DEC-002/003).
Secrets never reach the output: values under secret-like keys are replaced, and credentials
embedded in URLs or bearer tokens are masked inside any string value.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

REDACTED = "[REDACTED]"
SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential|dsn|private[_-]?key)", re.I
)
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<password>[^@\s/]+)@", re.I)
BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        value = URL_CREDENTIALS.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", value)
        return BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    if isinstance(value, MutableMapping):
        return {k: (REDACTED if isinstance(k, str) and SECRET_KEY.search(k) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    return value


def redact(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    return {k: (REDACTED if SECRET_KEY.search(k) else _scrub(v)) for k, v in event_dict.items()}


def configure_logging(environment: str, level: str = "INFO", stream: TextIO | None = None) -> None:
    def add_environment(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("environment", environment)
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            add_environment,
            structlog.processors.format_exc_info,
            redact,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level.upper()]),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
