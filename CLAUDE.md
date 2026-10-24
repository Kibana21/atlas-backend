# atlas-backend

Python backend for Atlas. It is a **modular monolith** (FastAPI API + Taskiq worker processes built from the same source and image family), not microservices. It owns identity/authorization, the canonical graph, reconciliation, validation, persistence, connectors, and jobs. Only this repo talks to PostgreSQL, Redis, Blob, Key Vault, and Azure discovery APIs.

Independent Git repo (`origin` = github.com/Kibana21/atlas-backend, **public**). Spec of record: `atlas-specs` (sibling checkout `../atlas-specs`), mainly `technical/Atlas_Technical_Architecture_System_Design_v1_1.md` (cited as §N below) and `features/ATLAS-001-engineering-foundation/spec.md`. This file is self-sufficient. A workspace-level file may exist locally, but it is not versioned.

## Status

ATLAS-001 Checkpoints 2 and 3 are merged:

- CP2 (B-01…B-08, B-27/B-28): app shell and settings with environment guards; correlation IDs, JSON logging with redaction, the error envelope and security headers; `/api/v1/health/live` and `/api/v1/diagnostics/build`; doctor, contract conformance and CI.
- CP3 (B-09…B-12): runtime-role async engine; `/health/ready` database check (still contract-pending until B-13 adds Redis); Alembic `0001_baseline`; migration and least-privilege integration tests; CI `integration` job on an isolated `postgres:15`.

The four Atlas roles and `atlas_test` exist on the local server (P-03 applied). Next is CP4 (B-13+: Redis/Taskiq), which needs **owner authorization**. Implement only approved task scope. Progress and evidence live in `atlas-specs` `features/ATLAS-001-engineering-foundation/evidence.md`.

## Commands

```bash
make setup              # uv sync --locked (project-local .venv) + copy .env/.env.test from the examples
make check              # lock-check, ruff lint + format-check, strict mypy, pytest (coverage >= 85%)
make contract           # extract the pinned atlas-specs (SPECS_DIR ?= ../atlas-specs), export OpenAPI, conformance
make security           # pip-audit of runtime deps + security/hygiene tests
make api API_PORT=8100  # dev server on 127.0.0.1; port 8000 is often taken by other local services
make migrate            # alembic upgrade head on atlas_db as atlas_migrator (.env)
make integration        # integration tests on atlas_test as the test roles (.env.test)
make doctor             # read-only preflight of local PostgreSQL/Redis (SNAPSHOT=file for an inventory)
make specs              # only the pinned-specs extraction + specs.lock validation
make export-openapi     # build/openapi.json
```

CI (`.github/workflows/backend-ci.yml`) has the required checks `static`, `unit`, `contract`, `security` and `integration`. They run on every PR and on push to `main`, and are never skipped. CI **never** connects to local PostgreSQL/Redis: `integration` uses its own digest-pinned `postgres:15` service with scram authentication, bootstrapped by `scripts/db/bootstrap.sql` (the approved proposal + one CI-only block, drift-checked).

## Workflow and approval boundaries

- Claude runs the whole workflow.
  1. Feature branch per approved task or checkpoint.
  2. Run `make check contract security integration` from a **fresh clone** of the committed branch.
  3. Push, then open a PR to `main`.
  4. Wait until every required check reports success.
  5. Review the diff against the acceptance criteria.
  6. Merge with a merge commit and a head-SHA guard.
  7. Confirm `main` CI is green and record evidence in `atlas-specs`.
- Never force-push, push to `main`, bypass branch protection, or merge failing or missing checks.
- Owner approval is required for:
  - material architecture or product changes;
  - destructive actions;
  - shared-infrastructure changes;
  - privileged access or credentials;
  - significant cost or scope changes;
  - unresolved spec conflicts;
  - the next checkpoint or gate.
- **Specs authority:**
  - `specs.lock` pins a full 40-hex `atlas-specs` SHA (currently the `contract-v0.1.0` commit). Release builds pin a released tag's commit.
  - The generated OpenAPI must match the pinned contract.
  - `contract-pending.yaml` lists only operations whose landing task has not merged. Each entry names its task, and the list is empty by Gate G5.
  - Never edit the contract here. Change `atlas-specs` first. If the specs conflict, stop and report.
- **Shared local services** (owner's PostgreSQL 15 and Redis, shared with other projects):
  - Read-only without explicit approval.
  - Never `FLUSHDB`/`FLUSHALL`; tests delete only keys they created.
  - Never change PostgreSQL authentication, roles, databases or configuration.
  - Never stop or restart them.
  - No Docker-based PostgreSQL and no PostgreSQL upgrade.
  - Atlas is limited to `atlas_db`/`atlas_test`, the four `atlas_*` roles, and Redis DB 10 (local) / 11 (test) with `atlas:local:`/`atlas:test:` prefixes. The settings guards enforce this.
  - Never stop processes Atlas did not start.
- **Public repo:**
  - Never commit secrets, tokens, credentials, confidential company or customer data, internal topology, or local-machine details (home paths, usernames, other projects' names).
  - `.env*` files are untracked, except the `*.example` templates, which hold placeholders only.
  - Review the staged diff before every push.
- **Credentials:** never print credentials in logs, commands or reports, and never forward `Authorization` headers to redirected storage or third-party hosts.
- **Evidence:** never claim tests or CI passed without the actual run, and never weaken tests, coverage or checks to get a pass. ATLAS-001 is not Accepted before G7.

## Stack (§0, ATLAS-001 approved)

Approved and in use:

- Python 3.12, uv (`uv.lock` committed, `exclude-newer` pinned);
- FastAPI, Pydantic v2;
- Ruff, strict mypy, pytest + coverage;
- structlog JSON logging with redaction and correlation IDs;
- hand-rolled UUIDv7 IDs.

Approved for later tasks:

- SQLAlchemy 2 + Alembic on **PostgreSQL 15** (local and CI);
- Redis + Taskiq, with PostgreSQL holding durable job state and the outbox;
- Entra ID OIDC;
- Azure SDK / Kubernetes client for read-only discovery.

Add dependencies only with pinned versions in `pyproject.toml` + `uv.lock`, and pin GitHub Actions by SHA.

Do **not** introduce Neo4j or another graph DB, Kafka, OpenSearch, or microservices without a benchmark-backed ADR (§0, §4.3, §14).

**Product scope exclusions** (BRD E20):

- PR impact analysis or GitHub PR integration;
- live telemetry or monitoring;
- distributed tracing;
- live health dashboards;
- inferred runtime traffic.

## Layout (§17.1; created incrementally)

```text
pyproject.toml  uv.lock  .env.example  specs.lock
src/atlas/
  main.py                 # FastAPI app composition
  api/routers|dependencies|schemas/   # transport layer; Pydantic DTOs versioned separately from DB models
  core/                   # settings, identity, security, errors, audit
  domain/                 # pure canonical entity/edge rules (no I/O)
  modules/                # catalog, projections, passports, reconciliation, discovery,
                          # architecture_design, contracts, rules, search, intelligence
  connectors/             # ARG, ARM, APIM, AKS, approved file adapters
  workers/                # Taskiq tasks, retries, job orchestration
  persistence/            # SQLAlchemy models, repositories, outbox
alembic/  fixtures/  scripts/  deploy/  .github/workflows/
tests/unit|integration|contract|security|performance/
```

Modules call each other through **typed Python service interfaces and DTOs, not HTTP**. Workers communicate via durable jobs with versioned payloads (§2.1). Respect each module's "must not do" column in §2.1 (for example, `catalog` never stores coordinates, `sources` never overwrites approved canonical values, `rules` never lets an LLM decide PASS/FAIL, `audit` is append-only).

## Core rules for backend code

**Data model (§3)**

- Canonical IDs are immutable UUIDs (v7 preferred), opaque in API paths. Provider identity is kept separately: normalized ARM ID, cluster + K8s UID, APIM gateway + API/revision/operation. External keys go through `source_identity`. A display name never proves identity.
- A logical service and its deployments are separate entities (`DEPLOYED_AS`, one-to-many). Prod and UAT are never merged.
- Keep three layers: immutable `source_observation` → canonical entity/relationship + `fact_provenance` (field-level) → presentation (`view_layout`). Trust state, lifecycle, and environment are orthogonal.
- Relationship validity is enforced against the versioned taxonomy in the service layer, plus DB constraints where practical. Direction is explicit. Cross-environment edges carry environment scope. *(The relationship-type list differs between BRD INV-02 and tech §3.3. Check `atlas-specs/decisions/` before hard-coding it.)*
- Snapshots use MVCC/revision manifests, not full-graph copies. A published baseline is immutable. A branch is typed patches against a pinned baseline.

**Projections and queries (§4)**

- Authorize scope **before** traversal or aggregation. Hidden counts must be access-aware, or omitted.
- Cap `node_budget`/`edge_budget`, depth (default max 4), `max_paths`, and wall-clock on the server, whatever the client sends. Return `truncated`, `hidden_count`/`bundle_count`, a continuation, and a signed short-lived `projection_token`.
- Bundles must never drop edges. Constituent relationships must be fetchable.

**API (§6)**

- Base `/api/v1`, generated OpenAPI that must match the pinned `atlas-specs` contract (CI diff), cursor pagination, and typed error envelope `{error:{code,message,correlation_id,details,recoverable}}`.
- Status codes: 401/403, 404 for hidden resources under the anti-enumeration policy, 409/412 for revision conflicts (`If-Match` on every save), 422, 429, 503. Writes take idempotency keys. Errors never expose SQL, hostnames, credentials, or restricted IDs.

**Discovery and reconciliation (§7)**

- Connectors are read-only and implement the `ArchitectureConnector` protocol (§7.4). Store the raw artifact + checksum → normalize → validate → reconcile. Sync is idempotent and keyed by source/scope/cursor/payload hash.
- A failed or partial sync never deletes anything. Unseen items go `stale` after a per-source grace policy, then tombstone.
- Matching order: provider ID → approved alias → org key → fuzzy candidates **only for steward review**. Same-precedence conflicts keep the prior approved value and open a `reconciliation_case`. Merges and splits are reversible.
- Never use Azure Monitor, App Insights, Log Analytics, OTel, or traffic to discover edges.

**Jobs (§8)**: assume at-least-once delivery, so handlers are idempotent. Commit canonical changes transactionally. Use the outbox for reindex/notify, exponential backoff with jitter, and a reviewable failed state. Advisory locks or unique active-run constraints prevent overlapping reconciliation. Losing Redis must not lose any committed fact or job history.

**Rules (§11)**: versioned deterministic evaluators over pinned snapshots, using allowlisted predicates. No user-supplied Python. Missing evidence or permission gives `UNKNOWN`. Exemptions have scope, approver, reason, and expiry, and never erase findings. The same rule + snapshot + config always gives the same result.

**AI (§13)**: provider-adapter interface (model configurable). Retrieval is authorization-scoped. Retrieved documents are data, not instructions. Outputs are schema-validated with citations. Proposed edits only go to a draft branch after human confirmation.

**Security (§9)**: validate the Entra token (issuer, audience, signature, expiry, tenant). Never trust frontend role claims. RBAC + ABAC (domain, app, environment, classification, source, action) with separation of duties. Treat every import/spec/XML/SVG/connector payload as hostile (XXE, SSRF, zip bombs, path traversal, XSS). Secrets live in Key Vault or managed identity, never in the graph, logs, prompts, audit, or exports. No tenant IDs, scopes, or roles in source code. Audit every authoritative edit, approval, reconciliation, exception, source config change, and sensitive export.

**Logging (§15)**: structured JSON with correlation IDs and redaction. Never log tokens, secrets, or sensitive architecture content.

## Testing expectations (§17.5, §20.3)

Ruff, strict typing, pytest, Hypothesis for graph invariants (duplicate-sync idempotency, cycles, direction, merge/split reversibility), Alembic migration tests, contract tests against the pinned OpenAPI, **negative authorization tests** (graph, search, AI, export), and malicious-input tests. Integration tests run against real PostgreSQL/Redis. Use synthetic fixtures under `fixtures/` only, never real tenant data. Scale fixtures go 250/700 → 10k/40k → 100k/500k (§4.4), with skewed hubs and cycles.

Migrations follow expand/migrate/contract so the old frontend keeps working. API and worker images ship from one commit.

## First slice (§19.1)

Sprint A: schema/identity, synthetic graph generator, API shell. Sprint B: scoped projection API, Passport, search. Sprint C: X-Ray expand, edge bundles, layout save/reload. Sprint D: draft commands, undo-able patches, 412 conflicts. **Don't start full connector work before canonical IDs and reconciliation semantics are reviewed.**
