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
| `make migrate` | Alembic `upgrade head` on `atlas_db` as `atlas_migrator` (`.env`) |
| `make integration` | Integration tests on `atlas_test` as the two test roles (`.env.test`) |
| `make worker` | Taskiq worker (one process) on the Atlas queue `atlas:local:taskiq:queue`, Redis DB 10 (`.env`). Stop with Ctrl+C / SIGTERM (graceful) |
| `make worker-smoke` | With `make worker` running: enqueue a ping, await its marker, delete it; prints `OK` |
| `make doctor` | **Read-only** preflight of the existing local PostgreSQL 15 and Redis (`SNAPSHOT=file` for an inventory) |
| `make contract` | Conformance against the pinned contract (needs a sibling `../atlas-specs` clone, or `SPECS_DIR=`) |
| `make security` | `pip-audit` of runtime dependencies + infrastructure-free security tests |

Local services are shared with other projects. Atlas uses only `atlas_db`/`atlas_test`, its own roles,
and Redis DBs 10/11 with `atlas:` key prefixes. It never flushes Redis or changes server configuration.

Prerequisite for `migrate`/`integration`: the four Atlas roles and `atlas_test` from the approved
`atlas-specs` proposal (`features/ATLAS-001-engineering-foundation/proposals/postgres-local-setup.sql`),
with their passwords in the git-ignored `.env` / `.env.test`. CI uses its own isolated `postgres:15`
service and `scripts/db/bootstrap.sql` (that proposal plus one CI-only `CREATE DATABASE atlas_db`).

## Migration policy

- **Roles:** Alembic connects with `ATLAS_MIGRATION_DATABASE_URL` only. The migration role owns schema
  `atlas`, `atlas.alembic_version` and every object it creates. The API and worker use the runtime role.
- **Reviewed:** every revision is code-reviewed. Autogenerate output is a draft, never merged unread.
  `alembic check` (no drift) and a single head are tested.
- **Reversible:** every `upgrade` has a working `downgrade`. Tests run `upgrade head -> downgrade base ->
  upgrade head` on `atlas_test`.
- **Expand / migrate / contract:** add new structures first, move data in a separate step, and remove
  old structures only after no deployed code uses them. The previous release keeps working at each step.
- **Least privilege:** the runtime role reads tables by default (schema default privileges). Any write
  privilege is granted **per table, explicitly**, in the migration that creates the table, to
  `app_role(context)` from `atlas.persistence.migrations` (`ATLAS_DB_APP_ROLE`). Never `GRANT ALL`,
  never schema-wide writes. Append-only tables never receive `UPDATE`/`DELETE`.
- Naming convention for constraints and indexes: `atlas.persistence.migrations.NAMING_CONVENTION`.

## Background tasks (Taskiq)

- **Processes:** producers (API, scripts, tests) only enqueue; the worker (`make worker`, i.e.
  `taskiq worker atlas.workers.entrypoint:broker`) only executes. `atlas.workers.entrypoint` is the
  only module that builds a broker at import; building one opens no connection and runs no task.
- **Registration:** explicit (`atlas.workers.broker.configure`). Middleware refuses unregistered task
  names and any queue other than `<prefix>taskiq:queue`.
- **Payloads:** typed and validated by the producer and again by the worker. Invalid arguments fail
  the task with the field names only. Argument values are never logged.
- **Correlation:** the producer stamps a `correlation_id` label; the worker binds it to logs for the task.
- **Failures:** logged once (`task_failed`, exception type) and never retried automatically. Tasks
  are idempotent. Retries, backoff, durable job state and the outbox are BL-201 (DEC-046).
- **Redis:** `atlas.persistence.redis` only. DB fixed by the URL (10 local, 11 test), keys under
  `atlas:`, exact-key deletion, bounded prefix scans, short timeouts. No server-wide or keyspace-wide
  commands (enforced by `tests/unit/test_redis_safety.py`). `scripts/redis_keys.py` gives read-only
  evidence (bounded `atlas:` scan, or a key count).
