"""Structured logging and redaction (FR-25, AC-10, AC-12)."""

from __future__ import annotations

import io
import json

import pytest
import structlog

from atlas.core.logging import configure_logging, get_logger

pytestmark = pytest.mark.unit


def _emit(**fields: object) -> dict[str, object]:
    stream = io.StringIO()
    configure_logging("test", stream=stream)
    get_logger("t").info("event-name", **fields)
    line = stream.getvalue().strip()
    parsed: dict[str, object] = json.loads(line)  # valid JSON, one line
    return parsed


def test_json_fields() -> None:
    out = _emit(answer=42)
    assert out["event"] == "event-name"
    assert out["level"] == "info"
    assert out["environment"] == "test"
    assert out["answer"] == 42
    assert str(out["timestamp"]).endswith("Z")


@pytest.mark.parametrize(
    "key", ["password", "db_password", "secret", "api_key", "apiKey", "token", "Authorization", "dsn", "private_key"]
)
def test_secret_like_keys_redacted(key: str) -> None:
    assert _emit(**{key: "s3cr3t-value"})[key] == "[REDACTED]"


def test_credentials_in_urls_and_bearer_values_redacted() -> None:
    out = _emit(
        url="postgresql+psycopg://atlas_app:hunter2@localhost:5432/atlas_db",
        header="Bearer abcdefghijklmnop",
        nested={"inner": {"password": "x", "url": "redis://user:pw@r:6379/10"}, "items": ["https://u:p@h/x"]},
    )
    assert out["url"] == "postgresql+psycopg://atlas_app:[REDACTED]@localhost:5432/atlas_db"
    assert out["header"] == "Bearer [REDACTED]"
    assert out["nested"] == {
        "inner": {"password": "[REDACTED]", "url": "redis://user:[REDACTED]@r:6379/10"},
        "items": ["https://u:[REDACTED]@h/x"],
    }
    assert "hunter2" not in json.dumps(out)


def test_correlation_id_from_context_is_logged() -> None:
    structlog.contextvars.bind_contextvars(correlation_id="req_test123456")
    try:
        assert _emit()["correlation_id"] == "req_test123456"
    finally:
        structlog.contextvars.clear_contextvars()


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging("test", level="WARNING", stream=stream)
    get_logger("t").info("hidden")
    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "text",
    [
        "connect failed secret=hunter2 retry",
        "password: hunter2",
        "DB_PASSWORD=hunter2;x=1",
        "api_key='hunter2'",
        "token=hunter2&a=b",
    ],
)
def test_secret_key_value_pairs_inside_text_redacted(text: str) -> None:
    out = _emit(message=text)
    assert "hunter2" not in json.dumps(out)
    assert "[REDACTED]" in str(out["message"])


def test_exception_traceback_is_kept_but_scrubbed() -> None:
    stream = io.StringIO()
    configure_logging("test", stream=stream)
    try:
        raise RuntimeError("login failed for user=atlas password=hunter2")
    except RuntimeError as exc:
        get_logger("t").error("boom", exc_info=exc)
    record = json.loads(stream.getvalue())
    assert "Traceback" in record["exception"]
    assert "hunter2" not in stream.getvalue()


def test_stdlib_records_and_library_tracebacks_are_redacted() -> None:
    import logging

    from atlas.core.logging import RedactingFilter, install_stdlib_redaction

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    lib = logging.getLogger("taskiq.receiver.test")
    lib.addHandler(handler)
    try:
        try:
            raise RuntimeError("connect redis://atlas:hunter2@host:6379/11 failed, password=hunter2")
        except RuntimeError:
            lib.error("Exception found while executing %s", "token abcdefgh12345678", exc_info=True)
    finally:
        lib.removeHandler(handler)
    text = stream.getvalue()
    assert "hunter2" not in text
    assert "abcdefgh12345678" not in text
    assert "RuntimeError" in text  # the traceback itself is kept
    root = logging.getLogger()
    before = list(root.handlers)
    install_stdlib_redaction()
    install_stdlib_redaction()  # idempotent
    assert all(sum(isinstance(f, RedactingFilter) for f in h.filters) == 1 for h in root.handlers)
    for h in root.handlers:
        if h not in before:
            root.removeHandler(h)
        else:
            h.filters = [f for f in h.filters if not isinstance(f, RedactingFilter)]
