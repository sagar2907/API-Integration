# TODO / Roadmap

Work happens in phase order. Each phase has an **exit criterion** — a demonstrable,
testable result. Do not start the next phase until the current one meets it.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

**Status: the 7-day MVP is complete.** See [README.md](README.md) for measured
results and an honest built-versus-planned table. The phases below remain the
long-term roadmap; the MVP implemented a deliberate subset of phases 1-8.

---

## Phase 0 — Foundations

- [x] Write `architecture.md`, `techstack.md`, `conventions.md`, `env.md`, `todo.md`
- [ ] `git init`, `.gitignore` (env files, binaries, `node_modules`, `__pycache__`, spec archives)
- [ ] `uv` workspace: `packages/core` plus `apps/{gateway,search,agent,worker,ingest}`, one `uv.lock`
- [ ] Repository skeleton: `packages/`, `apps/`, `web/`, `bench/`, `deploy/`
- [ ] `packages/core` skeleton: `config.py`, `errors.py`, `telemetry.py`, `models/`, `db/`, `http.py`
- [ ] `docker-compose.yml`: Postgres 16 + pgvector, Redis 7, OpenSearch 2.x
- [ ] Alembic initialized against `packages/core/db/`
- [ ] `Makefile`: `migrate`, `test`, `lint`, `bench`, `dev`, `run-*`, `ingest-samples`, `reindex`
- [ ] `.env.example` matching every variable in `env.md`
- [ ] `ruff` + `mypy --strict` config; both clean on the skeleton
- [ ] GitHub Actions: `uv sync`, ruff, mypy, pytest, plus web lint and vitest
- [ ] Health-check endpoints on gateway, search, and agent
- [ ] Next.js app scaffolded with the BFF route-handler pattern

**Exit criterion:** `uv sync`, `docker compose up`, and `make test` all succeed on a
clean clone.

## Phase 1 — API Knowledge Base

- [ ] SQLAlchemy models + Alembic migration for `providers`, `apis`, `endpoints`, `parameters`, `schemas`, `auth_schemes`
- [ ] Pydantic domain models in `packages/core/models/` mirroring them
- [ ] OpenAPI 3.x parser with `$ref` resolution; Swagger 2.0 conversion
- [ ] Normalizer: spec objects → internal records (methods, paths, params, bodies, responses)
- [ ] Auth-scheme extraction (`apikey`, `bearer`, `oauth2`, `basic`) with scopes
- [ ] Deduplication by `(provider, path, method)` and `spec_hash`
- [ ] Broken-spec detection: quarantine and report, never partially ingest
- [ ] Version tracking and deprecation flags
- [ ] `is_destructive` heuristic (DELETE, and known destructive POST operations)
- [ ] Ingestion run log: source, counts, errors, duration
- [ ] Seed corpus: GitHub, Slack, Stripe, SendGrid, Google Calendar
- [ ] Golden-file tests for the parser

**Exit criterion:** 500+ APIs and 5,000+ endpoints ingested; re-running ingestion is
idempotent (no duplicates, no drift).

## Phase 2 — Lexical Search

- [ ] OpenSearch index mapping with per-field analyzers and boosts
- [ ] Indexer: endpoint records → documents (summary, description, path tokens, tags, provider)
- [ ] Query normalization: lowercase, tokenize, expand action verbs (`send` → `post`, `create`)
- [ ] BM25 search with filters: provider, method, auth scheme, tags, `deprecated = false`
- [ ] `GET /v1/search` on the gateway, with cursor pagination
- [ ] Redis caching of hot queries
- [ ] Labeled query set: 50+ natural-language queries with relevant endpoint IDs
- [ ] Benchmark harness: Precision@K, Recall@K, MRR, NDCG@K, p50/p95/p99 latency
- [ ] Frontend: search page with filters and endpoint detail view

**Exit criterion:** p95 search latency under 500 ms on the full corpus, with a
recorded baseline for every retrieval metric.

## Phase 3 — Semantic & Hybrid Retrieval

- [ ] `pgvector` column + HNSW index; embedding backfill job
- [ ] Endpoint embedding text strategy (summary + description + path + tags)
- [ ] ANN search path
- [ ] Reciprocal-rank fusion of lexical and semantic results, weights configurable
- [ ] Capability and schema filters applied post-fusion
- [ ] Benchmark comparison: BM25 vs vector vs hybrid on the labeled set
- [ ] Document the winning configuration and why it wins

**Exit criterion:** hybrid retrieval measurably beats both baselines on NDCG@20, with
the numbers written into `bench/RESULTS.md`.

## Phase 4 — Workflow Representation

- [ ] Workflow DAG JSON schema (nodes, edges, mappings, metadata)
- [ ] Node types: `trigger`, `api_call`, `transform`, `condition`, `validate`, `approval`
- [ ] Migrations for `workflows`, `workflow_nodes`; insert-only versioning
- [ ] Workflow DAG as Pydantic models shared by the planner, compiler, and executor
- [ ] CRUD endpoints, plus export/import of a workflow definition
- [ ] React Flow editor: create, connect, configure, delete nodes
- [ ] Field-mapping UI showing upstream outputs and downstream required inputs
- [ ] Version history and rollback

**Exit criterion:** a workflow can be hand-built in the UI, saved, exported,
re-imported, and rolled back to a prior version.

## Phase 5 — Agentic Planning

- [ ] Requirement schema (Pydantic) — trigger, action, entities, fields, constraints, auth need
- [ ] Intent parser with structured output and strict validation
- [ ] Retrieval orchestration: one search per requirement slot
- [ ] Selection agent over top-K candidates
- [ ] Planner producing a DAG plus field mappings, referencing only `endpoint_id`s
- [ ] Bounded self-repair loop consuming structured validation errors
- [ ] Explanation agent: why these endpoints, how data flows
- [ ] Token and call-count accounting per plan
- [ ] Stubbed-client tests for every agent step

**Exit criterion:** the GitHub-issue-to-Slack goal from the specification produces a
draft workflow end to end, with no hallucinated endpoint surviving.

## Phase 6 — Deterministic Validation

- [ ] Structural checks: acyclic, single trigger, all nodes reachable
- [ ] Endpoint existence, active status, method and path agreement
- [ ] Required parameter coverage (path, query, header)
- [ ] Request-body validation against the endpoint's JSON Schema
- [ ] Auth requirement check and credential binding
- [ ] Schema compatibility per edge, including nested paths and type coercion rules
- [ ] Ambiguous- and missing-field detection with suggested mappings
- [ ] Policy checks: allowlist, destructive-action approval requirement
- [ ] Compile to the immutable IR; persist with a version
- [ ] Structured validation errors surfaced in the UI
- [ ] Comprehensive unit tests — this is the project's core, treat it that way

**Exit criterion:** an invalid workflow cannot reach the executor by any path, and
every rejection carries a machine-readable reason.

## Phase 7 — Execution Engine

- [ ] Migrations for `executions`, `execution_events`, `compiled_ir`
- [ ] Redis Streams producer and hand-written asyncio consumer-group worker
- [ ] Graceful shutdown: stop claiming, checkpoint in-flight nodes, exit clean
- [ ] Node executor: input resolution, request building from indexed metadata, dispatch
- [ ] Credential resolution at call time; encrypted store with envelope encryption
- [ ] Timeouts, retries with exponential backoff and jitter, `Retry-After` handling
- [ ] Per-provider token-bucket rate limiting in Redis
- [ ] Idempotency keys on side-effecting nodes
- [ ] Checkpoint-then-acknowledge, with resumption from the last completed node
- [ ] Dead-letter stream and a requeue path
- [ ] Circuit breaker per provider
- [ ] SSRF guard: host resolution, private-range rejection, allowlist enforcement
- [ ] Dry-run mode: build and validate the request, never dispatch
- [ ] Credential audit trail

**Exit criterion:** killing a worker mid-execution resumes without a duplicate
external side effect — proven by a test, not by inspection.

## Phase 8 — Verification & Observability

- [ ] Verification node: status code, response schema, contract comparison
- [ ] Side-effect confirmation where a read-back endpoint exists
- [ ] Error classification into the failure taxonomy
- [ ] Recovery agent: reads a classified failure, proposes a corrected plan (re-validated)
- [ ] OpenTelemetry traces spanning gateway → agent → worker → provider
- [ ] Prometheus metrics: search latency, compile time, execution success rate, provider failure rate, token cost
- [ ] Grafana dashboards
- [ ] Execution dashboard: run history, per-node timeline, retries, errors
- [ ] Fault-injection test suite: timeout, 429, 5xx, malformed body, worker kill

**Exit criterion:** a single trace ID follows one automation from the typed goal to
the verified provider response.

## Phase 9 — Scale & Deployment

- [ ] Webhook receiver with signature verification and a distributed lock against duplicate triggers
- [ ] Scheduled triggers
- [ ] Scale ingestion toward 5,000 APIs / 50,000 endpoints
- [ ] Load test search and execution independently
- [ ] Tune `UVICORN_WORKERS`, `WORKER_REPLICAS`, and pool sizes against measured load
- [ ] Kubernetes manifests; separate deployments for search and workers
- [ ] Production-like deployment with real observability
- [ ] `bench/RESULTS.md`: every metric, measured, dated

**Exit criterion:** the measured numbers table is complete and honest.

---

## Benchmark tracker

Fill the **Measured** column only from a real `make bench` run, and date it. Until
then it stays empty — including in any README or CV line.

| Metric | Target | Measured | Date |
|---|---|---|---|
| Indexed APIs | 5,000+ | | |
| Indexed endpoints | 50,000+ | | |
| Search latency p50 | < 200 ms | | |
| Search latency p95 | < 500 ms | | |
| Discovery latency p95 | < 1 s | | |
| Precision@20 | baseline then improve | | |
| NDCG@20 | baseline then improve | | |
| Workflow validation pass rate | > 90% | | |
| Execution success rate | > 95% | | |
| Required fields mapped correctly | > 95% | | |
| Recovery success after injected fault | > 90% | | |
| LLM tokens per generated workflow | track, then reduce | | |

## MVP line

Phases 0–7 constitute the MVP: 500–1,000 APIs, BM25 search, a simple DAG, 3–5
providers, API key and basic OAuth, manual execution, basic retries. Phases 8–9 are
what turn it into the flagship version. Ship the MVP end to end before broadening
any single layer.

## Parking lot

Ideas deliberately deferred — record them here rather than letting them expand a
phase in progress.

- Cross-encoder reranking on the top-50 candidates
- Multi-tenant workspaces and shared credentials
- Workflow templates and a library of common automations
- Streaming and paginated provider responses
- GraphQL and gRPC endpoint support alongside REST
- Cost-aware planning (prefer cheaper providers when equivalent)
