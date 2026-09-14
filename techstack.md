# Tech Stack

**Backend is Python end to end. Frontend is Next.js end to end.** No second backend
language. Every entry below states what it is for and why it was chosen over the
obvious alternative. If you want to change one, update this file in the same commit.

## Summary

| Layer | Choice | Purpose |
|---|---|---|
| Frontend | Next.js 15 (App Router) + React 19 + TypeScript + Tailwind | Search UI, workflow builder, execution dashboard |
| Graph editor | React Flow (`@xyflow/react`) | Visual DAG editing |
| Frontend BFF | Next.js route handlers | Session cookies, proxying to the gateway — no business logic |
| All backend services | Python 3.12 + FastAPI + Pydantic v2 | Gateway, search, agent, workflow engine, ingestion |
| ASGI runtime | `uvicorn` (dev), `gunicorn` + uvicorn workers (prod) | Process-based parallelism |
| Async I/O | `asyncio` + `httpx` | Concurrent provider calls, fan-out retrieval |
| Worker | Custom `asyncio` consumer over Redis Streams | Durable workflow execution |
| LLM | Claude (`claude-sonnet-5`; `claude-opus-5` for hard planning) | Structured reasoning steps |
| Primary DB | PostgreSQL 16 + SQLAlchemy 2.0 (async) + Alembic | APIs, schemas, workflows, executions |
| Lexical search | OpenSearch 2.x (`opensearch-py`) | BM25, filters, aggregations |
| Vector search | pgvector (HNSW) | Semantic retrieval |
| Queue | Redis Streams (`redis-py` asyncio) | Async execution, consumer groups, DLQ |
| Cache | Redis 7 | Hot metadata, rate-limit buckets, sessions |
| Secrets | AES-256-GCM envelope encryption (`cryptography`) in Postgres (dev) / cloud KMS (prod) | Credential storage |
| Observability | OpenTelemetry Python + Prometheus + Grafana + Tempo | Metrics, logs, traces |
| Packaging | `uv` workspace | One lockfile, shared internal packages |
| Containers | Docker + Docker Compose | Local dev, isolated execution |
| Deployment | Docker Compose, then Kubernetes | Local to production-like |
| CI | GitHub Actions | Lint, type-check, test, benchmark |

## Repository layout

A `uv` workspace: several deployable apps over one shared library.

```
/packages
  /core                 # shared, imported by every app
      config.py         # typed settings, validated at startup
      db/               # SQLAlchemy models, session factory, Alembic migrations
      models/           # Pydantic domain models — the single source of truth
      telemetry.py      # OTel setup, structured logging, redaction
      errors.py         # exception hierarchy + failure taxonomy
      http.py           # guarded httpx client (SSRF, timeouts, retries)
/apps
  /gateway              # FastAPI — public API, authn, quotas, routing
  /search               # FastAPI — BM25 + vector retrieval, fusion, filters
  /agent                # FastAPI — intent, selection, planning, recovery
  /worker               # asyncio process — Redis Streams consumer, node executor
  /ingest               # CLI — OpenAPI parsing, normalization, indexing
/web                    # Next.js
/bench                  # retrieval / validity / latency / recovery benchmarks
/deploy                 # compose, k8s, otel collector
```

Each app is independently deployable and independently scalable, which is what the
architecture requires — separate processes, not separate languages.

## Why these choices

**Python for every backend service.** The decisive argument is that the Pydantic
models in `packages/core/models/` are *the same objects* used by the planner to
produce structured output, by the validator to reject it, and by the executor to
build the request. In a split-language backend those contracts get defined twice and
drift; here a schema change is one edit and the type checker finds every caller.
Secondary benefits: one toolchain, one lockfile, one test runner, one way to
instrument, and the ecosystem the project leans on hardest — OpenAPI parsing,
embeddings, LLM SDKs, retrieval evaluation — is native to Python.

**Is `asyncio` enough for the execution engine?** Yes, because the workload is I/O
bound, not CPU bound. A worker spends its time waiting on provider HTTP responses,
and a single event loop holds thousands of those in flight. The GIL only bites on
CPU-bound work, and this project has exactly three such places — embedding
generation, large spec parsing, and JSON Schema compilation — all of which run in
the ingestion job or a process pool, never on a request path. Parallelism comes from
running more processes (uvicorn/gunicorn workers, more worker replicas), which is
also how you would scale a Go service horizontally. The rules that keep this true
are in [conventions.md](conventions.md#7-async-discipline) and are not optional: no
blocking call in an async path.

**FastAPI over Django or Flask.** Native async, Pydantic request/response validation
for free, dependency injection that makes testing trivial, and generated OpenAPI
docs for our own API — fitting, given the subject matter. Django brings an ORM and
admin we do not need and a synchronous heritage that fights the executor.

**A hand-written Redis Streams worker over Celery, arq, or Dramatiq.** The project's
core reliability claims are checkpoint-before-acknowledge, resumption from the last
completed node, and idempotent replay. Task frameworks own the acknowledgement
lifecycle, which is precisely the part that must be explicit here — and "how do you
handle a worker crash after the external call succeeded but before acknowledgement?"
is an interview question you want to answer with your own code. Redis Streams give
consumer groups, pending-entry lists, claim-after-timeout, and a natural dead-letter
pattern; the consumer loop is a few hundred lines. Celery would be the right call if
this were generic background jobs.

**SQLAlchemy 2.0 async + Alembic.** The metadata model is genuinely relational
(provider → api → endpoint → parameter/schema) and benefits from typed models and
real migrations. Escape hatch: the hot retrieval queries — pgvector ANN, ranking
joins — are written as raw SQL through the same connection, because the ORM adds
nothing there and hides the query plan.

**PostgreSQL as the single source of truth.** `JSONB` holds raw JSON Schemas and
workflow definitions without a second database; `pgvector` means semantic search
needs no extra service in the MVP.

**OpenSearch for BM25 rather than Postgres full-text.** The project's thesis is
retrieval quality: OpenSearch gives real BM25 tuning, per-field boosting, analyzers,
and `explain` output — what you need to make Precision@K and NDCG@K move for reasons
you can name. Postgres FTS is an acceptable fallback if running OpenSearch locally
becomes painful; record the switch here if you make it.

**pgvector over a dedicated vector DB.** One less service, transactional with the
metadata it indexes, and HNSW is fast enough at 50k endpoints. Revisit only if
measured recall or latency suffers.

**Redis Streams over Kafka.** Everything the execution engine needs, from a service
already present for caching and rate limits. Kafka's partition scale and durability
are not needed at this size and cost real operational complexity.

**Claude for the agent layer.** Strong structured output, which matters because every
response is parsed into a Pydantic model and validated before it can affect
anything. Model IDs live in config, never hardcoded.

**Next.js for the entire frontend.** App Router server components for the data-heavy
search and dashboard pages, client components for the builder. Route handlers serve
only as a backend-for-frontend: they hold the session cookie and proxy to the Python
gateway so the browser never carries a gateway token. Business logic never moves
into `/web`.

## Key libraries

### Python — shared

| Library | Use |
|---|---|
| `fastapi`, `uvicorn`, `gunicorn` | HTTP services and ASGI runtime |
| `pydantic` v2, `pydantic-settings` | Domain models, structured LLM output, typed config |
| `sqlalchemy` 2.0 (async), `alembic`, `psycopg[binary,pool]` v3 | Persistence and migrations |
| `redis` (asyncio) | Cache, rate-limit buckets, Streams |
| `httpx` | Async HTTP with a custom guarded transport |
| `tenacity` | Retry with exponential backoff and jitter |
| `structlog` | Structured JSON logging with redaction |
| `opentelemetry-sdk` + FastAPI/httpx/SQLAlchemy instrumentation | Traces and metrics |
| `cryptography` | AES-256-GCM envelope encryption for credentials |
| `pyjwt`, `authlib` | Sessions, OAuth 2.0 client flows |

### Python — per app

| App | Additional libraries |
|---|---|
| `ingest` | `prance` / `openapi-spec-validator` / `jsonref` (spec parsing and `$ref` resolution), `typer` (CLI) |
| `search` | `opensearch-py` (async), `sentence-transformers` or a hosted embedding client, `numpy` |
| `agent` | `anthropic`, `jinja2` (prompt templates) |
| `worker` | `jsonschema` (draft 2020-12 validation of provider payloads), `apscheduler` (scheduled triggers, Phase 9) |

Note the division of labor: **Pydantic** validates *our* internal contracts;
**`jsonschema`** validates payloads against *provider* schemas taken from OpenAPI
specs. They are not interchangeable.

Two things we deliberately write ourselves rather than take off the shelf, because
the available packages are per-process while these must be shared across workers:

- **Per-provider token-bucket rate limiting** — a Redis Lua script.
- **Circuit breakers** — breaker state in Redis, so one provider outage trips every
  worker rather than each discovering it separately.

### Tooling

| Tool | Use |
|---|---|
| `uv` | Dependency resolution, workspace, locking, virtualenvs |
| `ruff` | Lint and format (replaces black, isort, flake8) |
| `mypy --strict` | Type checking; CI-blocking |
| `pytest`, `pytest-asyncio`, `pytest-cov` | Tests |
| `respx` | Mock httpx transport — no CI test ever hits a network |
| `hypothesis` | Property tests for schema compatibility and mapping |
| `testcontainers` | Postgres/Redis/OpenSearch for integration tests |

### Frontend

| Library | Use |
|---|---|
| `next`, `react`, `typescript` | App shell |
| `tailwindcss` | Styling |
| `@xyflow/react` (React Flow) | Workflow DAG editor |
| `@tanstack/react-query` | Server state, polling execution status |
| `zod` | Runtime validation of gateway responses |
| `vitest`, `@testing-library/react`, `playwright` | Unit and end-to-end tests |

## Version policy

- Pin exact versions in lockfiles (`uv.lock`, `package-lock.json`). Commit them.
- Pin container images to a minor tag (`postgres:16`, `redis:7`), never `latest`.
- One dependency-bump commit at a time, with tests green.

## Adding a dependency

Answer these in the PR description before adding one:

1. What does it do that the standard library or an existing dependency does not?
2. Is it maintained (recent releases, open issue triage)?
3. What is the license? (Permissive only — MIT, Apache-2.0, BSD.)
4. What is the removal path if it is abandoned?

Reject dependencies that pull large transitive trees for small conveniences. And a
dependency is never a reason to introduce a second backend language.
