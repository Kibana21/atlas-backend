# atlas-backend developer commands (ATLAS-001 spec §8). Thin wrappers over uv.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
UV := uv run --quiet --locked
# Spec default 8000; override when another local service already uses it (e.g. `make api API_PORT=8100`).
API_PORT ?= 8000

.PHONY: setup lock-check lint format-check typecheck test check api

## setup: install the locked environment; create local env files from the examples if absent
setup:
	uv sync --locked
	@test -f .env || cp .env.example .env
	@test -f .env.test || cp .env.test.example .env.test

## lock-check: uv.lock matches pyproject.toml
lock-check:
	uv lock --check

lint:
	$(UV) ruff check .

format-check:
	$(UV) ruff format --check .

typecheck:
	$(UV) mypy

## test: unit tests with coverage (fail_under = 85)
test:
	$(UV) pytest -m "unit or contract or security" --cov --cov-report=term

## check: everything CI's static + unit jobs run, without external services
check: lock-check lint format-check typecheck test
	@echo "make check: all backend gates passed"

## api: run the API locally with reload on 127.0.0.1:$(API_PORT) (settings from .env)
api:
	ATLAS_ENV_FILE=.env $(UV) uvicorn --factory atlas.main:create_app_from_env --reload --host 127.0.0.1 --port $(API_PORT)
