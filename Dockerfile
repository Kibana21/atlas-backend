# syntax=docker/dockerfile:1
# atlas-backend images (ATLAS-001 FR-37, B-21): targets `api` and `worker` from one source tree.
# Pinned by digest; non-root; no credentials or env files baked in (configuration is ATLAS_* at run time).
#   docker build --target api    --build-arg ATLAS_BUILD_SHA=$(git rev-parse HEAD) -t atlas-api .
#   docker build --target worker --build-arg ATLAS_BUILD_SHA=$(git rev-parse HEAD) -t atlas-worker .

ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f

FROM ${UV_IMAGE} AS uv

# ---- build: resolve the locked runtime dependencies into /app/.venv (no dev group) ----
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

# ---- runtime base: the repository layout under /app, owned by root, run as an unprivileged user ----
FROM ${PYTHON_IMAGE} AS runtime
# libpq from Debian (security-maintained) instead of the psycopg-binary wheel's bundled C libraries.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libpq5=17.11-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 atlas \
    && useradd --system --uid 10001 --gid atlas --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin atlas
ARG ATLAS_BUILD_SHA=unknown
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATLAS_BUILD_SHA=${ATLAS_BUILD_SHA} \
    ATLAS_ENV_FILE=/nonexistent
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/src ./src
COPY pyproject.toml specs.lock alembic.ini ./
COPY alembic ./alembic
USER 10001:10001
STOPSIGNAL SIGTERM

# ---- api: uvicorn on 0.0.0.0:8000 inside the container (publish as needed) ----
FROM runtime AS api
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "--factory", "atlas.main:create_app_from_env", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10", "--no-server-header"]

# ---- worker: one Taskiq worker process; healthy while it can reach its Redis DB ----
FROM runtime AS worker
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "atlas.workers.healthcheck"]
CMD ["taskiq", "worker", "atlas.workers.entrypoint:broker", "--workers", "1", "--max-async-tasks", "10", "--shutdown-timeout", "10", "--wait-tasks-timeout", "10"]
