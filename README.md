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
