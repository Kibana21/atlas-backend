# atlas-backend

Python backend for Atlas. It is a **modular monolith** (FastAPI API + Taskiq worker processes built from the same source and image family), not microservices. It owns identity/authorization, the canonical graph, reconciliation, validation, persistence, connectors, and jobs. Only this repo talks to PostgreSQL, Redis, Blob, Key Vault, and Azure discovery APIs.

Independent Git repo (`origin` = github.com/Kibana21/atlas-backend). Workspace overview and product invariants: `../CLAUDE.md`. Spec of record: `../atlas-specs/technical/Atlas_Technical_Architecture_System_Design_v1_1.md` (cited as §N below).

## Status

Empty scaffold: only `README.md` and `.gitignore` (Python). No `pyproject.toml`, code, migrations, or CI yet. When you create the first pieces, follow the layout below. Don't invent commands in this file until they exist. Add a **Commands** section once `pyproject.toml` is in place.

Undecided tooling (confirm with the user and record in `atlas-specs` ADRs): `uv` vs another locker, mypy vs pyright (strict either way), Azure Container Apps vs AKS, and the exact Python 3.12+ patch.

## Stack (proposed, §0)

FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · PostgreSQL 16+ (JSONB + typed tables, recursive CTEs for bounded traversal, FTS + trigram search) · Redis + Taskiq (PostgreSQL holds durable job state + outbox) · Entra ID OIDC · Azure SDK / Kubernetes client for read-only discovery.
Do **not** introduce Neo4j or another graph DB, Kafka, OpenSearch, or microservices without a benchmark-backed ADR (§0, §4.3, §14).

## Target layout (§17.1)

```
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
