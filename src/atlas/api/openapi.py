"""OpenAPI generation aligned with the contract dialect (contracts/style-guide.md, spec §6.7).

FastAPI does not emit response headers set by middleware, so this hook adds the shared
``X-Correlation-ID`` header component and references it from every response.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from atlas.core.correlation import HEADER

CORRELATION_HEADER = {
    "description": "Correlation ID of the request. Echoes a valid inbound value, otherwise a server-generated one.",
    "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,64}$"},
}


def install_openapi(app: FastAPI) -> None:
    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes, description=app.description)
        schema.setdefault("components", {}).setdefault("headers", {})[HEADER] = CORRELATION_HEADER
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                for response in operation.get("responses", {}).values():
                    response.setdefault("headers", {})[HEADER] = {"$ref": f"#/components/headers/{HEADER}"}
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
