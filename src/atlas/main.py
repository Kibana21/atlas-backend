"""FastAPI application factory (ATLAS-001 FR-22).

Middleware order, outermost first: correlation ID -> security headers -> CORS -> unhandled errors.
Every response (including CORS preflight and 500s) therefore carries X-Correlation-ID and the
baseline security headers. The OpenAPI schema is identical in every environment; only the
interactive docs are limited to local.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atlas.api.openapi import install_openapi
from atlas.api.routers import health
from atlas.api.schemas.common import ErrorEnvelope
from atlas.core.correlation import HEADER, CorrelationIdMiddleware
from atlas.core.errors import UnhandledErrorMiddleware, register_error_handlers
from atlas.core.logging import configure_logging
from atlas.core.security_headers import SecurityHeadersMiddleware
from atlas.core.settings import Settings, get_settings
from atlas.version import contract_version

API_PREFIX = "/api/v1"


def build_api_router() -> APIRouter:
    router = APIRouter(
        prefix=API_PREFIX,
        responses={500: {"model": ErrorEnvelope, "description": "Unexpected server error."}},
    )
    router.include_router(health.router)
    return router


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.env.value, settings.log_level)
    app = FastAPI(
        title="Atlas API",
        version=contract_version(),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.include_router(build_api_router())
    register_error_handlers(app)
    install_openapi(app)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET"],
        allow_headers=[HEADER],
        expose_headers=[HEADER],
        allow_credentials=False,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    return app


def create_app_from_env() -> FastAPI:
    """Entry point for uvicorn (`--factory`): settings from ATLAS_* / ATLAS_ENV_FILE."""
    return create_app(get_settings())
