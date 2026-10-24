# atlas-backend

Atlas backend: a FastAPI modular monolith (Python 3.12) with a Taskiq worker built from the same
source. It implements the contract pinned in `specs.lock` (`atlas-specs` release `contract-v0.1.0`).

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.10.x | Installs the pinned Python 3.12 environment from `uv.lock` into `.venv` |
| PostgreSQL | **15**, already running locally | Shared server. Atlas needs the four Atlas roles and `atlas_test` (below) |
| Redis | 7.4, already running locally | Shared server. Atlas uses only DB **10** (local) and DB **11** (tests) |
| Docker | any recent | Only for contract tools, scans and image builds. Never used for local PostgreSQL |
| `atlas-specs` | sibling clone `../atlas-specs` | Read-only input for `make contract` (or pass `SPECS_DIR=`) |

## Environment setup

```bash
make setup        # uv sync --locked; creates .env and .env.test from the examples (placeholders only)
```

`.env` (local) and `.env.test` (tests) are git-ignored. Replace the `change-me` placeholders with the
real role passwords. Never commit them, paste them into issues, or put them in shell history.

| Variable | Local (`.env`) | Tests (`.env.test`) |
|---|---|---|
| `ATLAS_ENV` | `local` | `test` |
| `ATLAS_DATABASE_URL` (runtime role) | `…atlas_app:…@localhost:5432/atlas_db` | `…atlas_test_app:…@localhost:5432/atlas_test` |
| `ATLAS_MIGRATION_DATABASE_URL` | `…atlas_migrator:…@…/atlas_db` | `…atlas_test_migrator:…@…/atlas_test` |
| `ATLAS_DB_APP_ROLE` | `atlas_app` | `atlas_test_app` |
| `ATLAS_REDIS_URL` | `redis://localhost:6379/10` | `redis://localhost:6379/11` |
| `ATLAS_REDIS_KEY_PREFIX` | `atlas:local:` | `atlas:test:` (tests add a per-run suffix) |
| `ATLAS_CORS_ORIGINS`, `ATLAS_DIAGNOSTICS_ENABLED`, `ATLAS_LOG_LEVEL`, `ATLAS_BUILD_SHA` | optional | optional |

Settings are validated at startup:

- the test environment must use `atlas_test`, Redis DB 11 and an `atlas:test:` prefix;
- local must use DB 10 and `atlas:local:`;
- diagnostics are refused in uat/prod.

## Local PostgreSQL 15 and Redis

**PostgreSQL:** the four roles and `atlas_test` come from the reviewed `atlas-specs` proposal
`features/ATLAS-001-engineering-foundation/proposals/postgres-local-setup.sql`, applied once by the
server owner. It aborts without changes if anything already exists.

| Role | Purpose |
|---|---|
| `atlas_migrator` | Runs migrations on `atlas_db`; owns schema `atlas` |
| `atlas_app` | Runtime role on `atlas_db`: read by default; writes only via explicit per-table grants |
| `atlas_test_migrator` | As `atlas_migrator`, for `atlas_test` only |
| `atlas_test_app` | As `atlas_app`, for `atlas_test` only |

Neither environment's roles can connect to the other environment's database.

**Redis:** no setup. Atlas keys always start with `atlas:` in DB 10 or 11.

`make doctor` checks all of this **read-only**: server major, roles, databases, schema, Redis DBs,
and the authentication method.

## Running

| Command | What it does |
|---|---|
| `make api API_PORT=8100` | API on `127.0.0.1:<port>` with reload (default 8000; pick another port if 8000 is in use) |
| `make migrate` | Alembic `upgrade head` on `atlas_db` as `atlas_migrator`; check with `ATLAS_ENV_FILE=.env uv run alembic current` |
| `make worker` | Taskiq worker (one process) on `atlas:local:taskiq:queue`, Redis DB 10. Ctrl+C / SIGTERM stops it gracefully |
| `make worker-smoke` | With `make worker` running: enqueue a ping, await the marker, delete it; prints `OK` |

Endpoints:

- `GET /api/v1/health/live`;
- `GET /api/v1/health/ready` (PostgreSQL + Redis, 1 s each);
- `GET /api/v1/diagnostics/build` (local/dev/test only).

`/docs` is served in `local` only.

## Tests and checks

| Command | What it does |
|---|---|
| `make check` | Lock check, Ruff lint + format, strict mypy, unit tests (coverage ≥ 85%). No external services |
| `make integration` | Integration tests on `atlas_test` and Redis DB 11 (`.env.test`): migrations, least privilege, readiness, worker round trip. Guards fail if anything outside Atlas changed |
| `make contract` | Pinned specs → OpenAPI export → structural conformance → drills and response-body validation against `contract-v0.1.0` |
| `make security` | `pip-audit` of runtime dependencies + security tests (secret patterns, Redis safety, workflow rules) |
| `make secret-scan` | Dedicated Trivy secret scan of tracked files |
| `make images` | Build and verify the Docker images (below) |

CI (`.github/workflows/backend-ci.yml`) runs the required jobs `static`, `unit`, `contract`,
`security`, `integration` and `images` on every PR and on `main`. CI never uses the local services;
`integration` and `images` use their own containers.

## Docker images

```bash
make images                                   # build + scan + verify both images (scripts/images_check.sh)
docker build --target api    --build-arg ATLAS_BUILD_SHA="$(git rev-parse HEAD)" -t atlas-api .
docker build --target worker --build-arg ATLAS_BUILD_SHA="$(git rev-parse HEAD)" -t atlas-worker .
docker run --rm -p 127.0.0.1:8100:8000 --env-file <your-env-file> atlas-api
docker run --rm --env-file <your-env-file> atlas-worker
docker run --rm --env-file <your-env-file> atlas-api alembic upgrade head   # migrations
```

- **Base images:** pinned by digest. Python dependencies come from `uv.lock` without the dev group; libpq is Debian's `libpq5`.
- **Runtime:** runs as uid 10001. No env files, credentials, tests, caches or VCS data in the image; all configuration is `ATLAS_*` at run time.
- **Hardened runtime:** works with `--read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp`.
- **Health checks:** `api` checks `/health/live`; `worker` pings its Redis DB.
- **Shutdown:** SIGTERM (`docker stop`) is graceful for both and exits 0.
- **Database URLs:** a container cannot reach `localhost` services on the host. Use host names that resolve from the container, e.g. Docker's host gateway. Never point an image at another project's database or Redis DB.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `SettingsError: test runs must use database 'atlas_test'…` | Wrong env file for the command. Tests use `.env.test`, the API/worker use `.env` |
| `make doctor` FAIL `postgres.roles` | The roles were not created yet. See the proposal SQL above (owner action) |
| `make doctor` WARN `trust` | The local server does not check passwords (DEC-084). Privileges still apply; CI enforces passwords |
| `/health/ready` 503 `failed_check: redis` (or `database`) | That dependency did not answer within 1 s. Check it is running and that the URL/DB index is right |
| Port 8000 in use | `make api API_PORT=8100` |
| `make worker-smoke` FAIL "no marker" | Start `make worker` first (same `.env`) |
| `make contract` "pinned contract missing" | Clone `atlas-specs` next to this repo, or pass `SPECS_DIR=` |

## Security restrictions

- **Public repository:** never commit secrets, real infrastructure details or customer data. `make security` and `make secret-scan` check tracked files.
- **Shared PostgreSQL and Redis:** Atlas touches only `atlas_db`/`atlas_test`, its four roles, and `atlas:` keys in DB 10/11. Never flush Redis, never list, read or delete other keys, and never change server settings, authentication, roles or databases without the owner's approval.
- **No Docker-based PostgreSQL for local development:** the images and CI use containers only for isolated packaging and tests.
- **Logs and errors:** logs are redacted (credentials, tokens, DSNs, library tracebacks). Error responses never include hosts, SQL, paths or stack traces.

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
