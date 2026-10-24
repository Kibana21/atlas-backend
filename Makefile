# atlas-backend developer commands (ATLAS-001 spec §8). Thin wrappers over uv.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
UV := uv run --quiet --locked
# Spec default 8000; override when another local service already uses it (e.g. `make api API_PORT=8100`).
API_PORT ?= 8000

.PHONY: setup lock-check lint format-check typecheck test check api migrate integration worker worker-smoke images secret-scan doctor specs export-openapi contract security

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
	$(UV) pytest -m "(unit or contract or security) and not integration and not pinned_specs" --cov --cov-report=term

## check: everything CI's static + unit jobs run, without external services
check: lock-check lint format-check typecheck test
	@echo "make check: all backend gates passed"

## api: run the API locally with reload on 127.0.0.1:$(API_PORT) (settings from .env)
api:
	ATLAS_ENV_FILE=.env $(UV) uvicorn --factory atlas.main:create_app_from_env --reload --host 127.0.0.1 --port $(API_PORT)

## migrate: Alembic upgrade head on atlas_db as the migration role (settings from .env)
migrate:
	ATLAS_ENV_FILE=.env $(UV) alembic upgrade head

## integration: integration tests against the local atlas_test database as the test roles (.env.test)
integration:
	ATLAS_ENV_FILE=.env.test $(UV) pytest -m integration --no-cov

## worker: Taskiq worker (one process) on the Atlas queue <prefix>taskiq:queue (settings from .env)
worker:
	ATLAS_ENV_FILE=.env $(UV) taskiq worker atlas.workers.entrypoint:broker --workers 1 --max-async-tasks 10 --shutdown-timeout 10 --wait-tasks-timeout 10

## worker-smoke: with `make worker` running, enqueue a ping on DB 10, await the marker, delete it
worker-smoke:
	ATLAS_ENV_FILE=.env $(UV) python scripts/worker_smoke.py

## images: build api/worker images, scan (hadolint, Trivy), verify non-root/contents and a hardened runtime
images:
	scripts/images_check.sh

## secret-scan: dedicated secret scan of tracked files (Trivy, digest-pinned; read-only mount)
TRIVY_IMAGE = aquasec/trivy:0.67.2@sha256:e2b22eac59c02003d8749f5b8d9bd073b62e30fefaef5b7c8371204e0a4b0c08
secret-scan:
	@rm -rf build/secret-scan && mkdir -p build/secret-scan
	git ls-files -z | xargs -0 tar -cf - | tar -xf - -C build/secret-scan
	docker run --rm -v "$(CURDIR)/build/secret-scan:/repo:ro" $(TRIVY_IMAGE) fs --quiet --scanners secret --exit-code 1 /repo

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

## contract: structural conformance against the pinned contract, then drills and response-body validation
contract: specs export-openapi
	$(UV) python scripts/contract_subset.py build/specs/contracts/openapi/atlas-api.yaml contract-pending.yaml build/contract-compared.json
	uv run --quiet --locked --project build/specs/tools python build/specs/tools/contract_conformance.py conform \
		--contract build/contract-compared.json --implementation build/openapi.json
	$(UV) pytest -m pinned_specs --no-cov -q -ra   # B-19 drills + B-20 response validation (pinned contract)

## security: dependency vulnerability audit + infrastructure-free security/config tests
security:
	@mkdir -p build
	uv export --quiet --locked --no-dev --no-emit-project --format requirements-txt > build/requirements-runtime.txt
	$(UV) pip-audit --strict --requirement build/requirements-runtime.txt --disable-pip
	$(UV) pytest -m "security and not integration and not pinned_specs" -q --no-cov
