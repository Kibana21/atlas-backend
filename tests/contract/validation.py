"""Validate real responses against the pinned contract (status, headers, content type, body schema)."""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ContractViolationError(AssertionError):
    pass


def operation(contract: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    op = contract["paths"].get(path, {}).get(method.lower())
    if op is None:
        raise ContractViolationError(f"{method} {path} is not a contract operation")
    return dict(op)


def _deref(contract: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        parts = node["$ref"].removeprefix("#/").split("/")
        target: Any = contract
        for part in parts:
            target = target[part]
        node = target
    return node


def _strict(contract: dict[str, Any], schema: dict[str, Any], body: Any, where: str) -> None:
    """No undeclared properties, unless the contract allows them (``additionalProperties: true``)."""
    schema = _deref(contract, schema)
    if isinstance(body, dict) and "properties" in schema:
        if schema.get("additionalProperties") is not True:
            extra = set(body) - set(schema["properties"])
            if extra:
                raise ContractViolationError(f"{where}: undeclared properties {sorted(extra)}")
        for key, value in body.items():
            if key in schema["properties"]:
                _strict(contract, schema["properties"][key], value, f"{where}.{key}")
    for keyword in ("anyOf", "oneOf"):
        for option in schema.get(keyword, []):
            option = _deref(contract, option)
            if option.get("type") == "object" or "properties" in option:
                _strict(contract, option, body, where)


def validate_response(contract: dict[str, Any], method: str, path: str, status: int, headers: Any, body: Any) -> str:
    """Raise ContractViolationError unless the response matches the contract. Returns the schema component."""
    op = operation(contract, method, path)
    declared = op["responses"].get(str(status))
    if declared is None:
        raise ContractViolationError(f"{method} {path}: status {status} not declared ({sorted(op['responses'])})")
    content = declared["content"]
    media = headers.get("content-type", "").split(";")[0].strip()
    if media not in content:
        raise ContractViolationError(f"{method} {path} {status}: content type {media!r} not in {sorted(content)}")
    for name, header in declared.get("headers", {}).items():
        value = headers.get(name)
        if value is None:
            raise ContractViolationError(f"{method} {path} {status}: missing header {name}")
        pattern = _deref(contract, header)["schema"].get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            raise ContractViolationError(f"{method} {path} {status}: header {name}={value!r} violates {pattern}")
    ref = content[media]["schema"]
    schema = {"$schema": DIALECT, **ref, "components": contract["components"]}
    try:
        Draft202012Validator(schema).validate(body)
    except ValidationError as exc:
        raise ContractViolationError(f"{method} {path} {status}: {exc.message} at {list(exc.absolute_path)}") from None
    _strict(contract, ref, body, f"{method} {path} {status}")
    return str(ref["$ref"]).rsplit("/", 1)[-1]


def validate_json_schema(schema: dict[str, Any], body: Any) -> None:
    try:
        Draft202012Validator(schema).validate(body)
    except ValidationError as exc:
        raise ContractViolationError(f"JSON Schema {schema.get('$id')}: {exc.message}") from None
