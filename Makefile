# atlas-backend developer commands (ATLAS-001 spec §8). Thin wrappers over uv.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
UV := uv run --quiet --locked
# Spec default 8000; override when another local service already uses it (e.g. `make api API_PORT=8100`).
API_PORT ?= 8000

.PHONY: setup lock-check lint format-check typecheck test check api doctor specs export-openapi contract

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

## doctor: read-only preflight of the existing PostgreSQL/Redis (SNAPSHOT=file for an inventory)
doctor:
	ATLAS_ENV_FILE=.env $(UV) python -m atlas.tools.doctor $(if $(SNAPSHOT),--snapshot $(SNAPSHOT),)

## specs: extract contracts/ and tools/ from atlas-specs at the pinned SHA; validate the lock (release mode)
SPECS_DIR ?= ../atlas-specs
SPECS_SHA = $(shell awk -F'"' '/^specs_commit:/{print $$2}' specs.lock)
specs:
	@test -n "$(SPECS_SHA)" || { echo "specs.lock has no specs_commit"; exit 1; }
	git -C "$(SPECS_DIR)" cat-file -e "$(SPECS_SHA)^{commit}"
	rm -rf build/specs && mkdir -p build/specs
	git -C "$(SPECS_DIR)" archive "$(SPECS_SHA)" contracts tools | tar -x -C build/specs
	uv run --quiet --locked --project build/specs/tools python build/specs/tools/check_specs_lock.py specs.lock --specs-dir "$(SPECS_DIR)" --release

## export-openapi: deterministic OpenAPI of the app -> build/openapi.json
export-openapi:
	@mkdir -p build
	$(UV) python scripts/export_openapi.py build/openapi.json

## contract: conformance of the app against the pinned contract minus contract-pending.yaml
contract: specs export-openapi
	$(UV) python scripts/contract_subset.py build/specs/contracts/openapi/atlas-api.yaml contract-pending.yaml build/contract-compared.json
	uv run --quiet --locked --project build/specs/tools python build/specs/tools/contract_conformance.py conform \
		--contract build/contract-compared.json --implementation build/openapi.json
