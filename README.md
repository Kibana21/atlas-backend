# atlas-backend

Atlas backend: a FastAPI modular monolith (Python 3.12) with Taskiq workers (later checkpoints).
Implements the contract pinned in `specs.lock` (`atlas-specs` release `contract-v0.1.0`).
Full local-development guide: ATLAS-001 task B-25.

## Commands

| Command | What it does |
|---|---|
| `make setup` | `uv sync --locked`; creates `.env` / `.env.test` from the examples (placeholders only) |
| `make check` | Lock check, Ruff lint + format, strict mypy, unit tests with coverage ≥ 85% |
| `make api` | API on `127.0.0.1:$(API_PORT)` (default 8000; e.g. `make api API_PORT=8100` if 8000 is taken) |
| `make doctor` | **Read-only** preflight of the existing local PostgreSQL 15 and Redis (`SNAPSHOT=file` for an inventory) |
| `make contract` | Conformance against the pinned contract (needs a sibling `../atlas-specs` clone, or `SPECS_DIR=`) |
| `make security` | `pip-audit` of runtime dependencies + infrastructure-free security tests |

Local services are shared with other projects. Atlas uses only `atlas_db`/`atlas_test`, its own roles,
and Redis DBs 10/11 with `atlas:` key prefixes. It never flushes Redis or changes server configuration.
