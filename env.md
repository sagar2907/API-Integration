# Environment & Configuration

Everything needed to run this project locally, and the meaning of every environment
variable. When you add a variable, document it here and add it to `.env.example` in
the same commit.

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | latest | Postgres, Redis, OpenSearch, Grafana all run in containers |
| Python | 3.12+ | every backend service |
| `uv` | latest | workspace, dependency resolution, virtualenv — the only Python tooling you need |
| Node.js | 20 LTS+ | frontend |
| `make` | any | task runner |

`uv sync` at the repository root creates one virtualenv covering every app and
`packages/core`. Do not create per-app virtualenvs.

On Windows, run `uv` and Node commands from PowerShell; the `make` targets assume
Git Bash or WSL. If `make` is unavailable, the underlying commands are listed in the
`Makefile` and can be run directly.

## 2. First-time setup

```bash
cp .env.example .env          # then fill in the values marked REQUIRED
uv sync                       # one virtualenv for the whole workspace
docker compose -f deploy/docker-compose.yml up -d
make migrate                  # alembic upgrade head
make ingest-samples           # load the bundled OpenAPI specs
make test                     # confirm a clean baseline
```

Then, in separate terminals:

```bash
make run-gateway              # :8080  uvicorn --reload
make run-search               # :8001  uvicorn --reload
make run-agent                # :8000  uvicorn --reload
make run-worker               # no port; consumes the queue
cd web && npm run dev         # :3000
```

`make dev` starts all four backend processes under one supervisor if you would
rather not manage terminals.

## 3. Local service ports

| Service | Port | URL |
|---|---|---|
| Frontend | 3000 | http://localhost:3000 |
| Gateway | 8080 | http://localhost:8080/v1 |
| Agent service | 8000 | http://localhost:8000 |
| Search service | 8001 | http://localhost:8001 |
| PostgreSQL | 5432 | — |
| Redis | 6379 | — |
| OpenSearch | 9200 | http://localhost:9200 |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3001 | http://localhost:3001 |

## 4. Environment variables

### Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_ENV` | yes | `development` | `development`, `test`, `staging`, `production`. Gates debug logging and dev-only auth shortcuts. |
| `LOG_LEVEL` | no | `info` | `debug`, `info`, `warn`, `error`. |
| `GATEWAY_PORT` | no | `8080` | Gateway listen port. |
| `AGENT_PORT` | no | `8000` | Agent service listen port. |
| `SEARCH_PORT` | no | `8001` | Search service listen port. |
| `AGENT_SERVICE_URL` | yes | `http://localhost:8000` | How the gateway reaches the agent service. |
| `SEARCH_SERVICE_URL` | yes | `http://localhost:8001` | How the gateway and agent reach the search service. |
| `GATEWAY_URL` | yes | `http://localhost:8080` | Used by the Next.js BFF to proxy requests. Server-side only — never exposed to the browser. |
| `UVICORN_WORKERS` | no | `1` (dev) / cores (prod) | Processes per ASGI app. Parallelism is process-based; see [architecture.md](architecture.md#7-runtime-and-scaling-posture). |

### Database

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Async driver URL: `postgresql+psycopg://user:pass@localhost:5432/apiengine`. Alembic uses the same value. |
| `DATABASE_MAX_CONNS` | no | `20` | Pool ceiling **per process** — multiply by `UVICORN_WORKERS` and `WORKER_REPLICAS` when sizing Postgres `max_connections`. |
| `DATABASE_STATEMENT_TIMEOUT_MS` | no | `5000` | Server-side statement timeout. |

### Search

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENSEARCH_URL` | yes | `http://localhost:9200` | Lexical index endpoint. |
| `OPENSEARCH_INDEX` | no | `endpoints_v1` | Index name; bump the suffix for a reindex. |
| `SEARCH_TOP_K` | no | `20` | Candidate set size returned to the agent. |
| `SEARCH_BM25_WEIGHT` | no | `0.5` | Fusion weight for lexical results. |
| `SEARCH_VECTOR_WEIGHT` | no | `0.5` | Fusion weight for semantic results. |
| `SEARCH_CACHE_TTL_SECONDS` | no | `300` | Redis TTL for hot query results. |

### Embeddings

| Variable | Required | Default | Description |
|---|---|---|---|
| `EMBEDDING_PROVIDER` | yes (Phase 3+) | `local` | `local` (sentence-transformers) or a hosted provider. |
| `EMBEDDING_MODEL` | yes (Phase 3+) | — | Model identifier. |
| `EMBEDDING_DIMENSIONS` | yes (Phase 3+) | `768` | Must match the `pgvector` column definition. |

### LLM / agent

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes (Phase 5+) | — | **Secret.** Claude API key. |
| `LLM_MODEL_INTENT` | no | `claude-sonnet-5` | Model for intent parsing. |
| `LLM_MODEL_PLANNER` | no | `claude-opus-5` | Model for workflow planning. |
| `LLM_MAX_TOKENS` | no | `4096` | Per-call output ceiling. |
| `LLM_TIMEOUT_SECONDS` | no | `60` | Per-call timeout. |
| `PLANNER_MAX_REPAIR_ITERATIONS` | no | `3` | Validator feedback loops before giving up. |
| `LLM_DAILY_TOKEN_BUDGET` | no | `1000000` | Hard stop to prevent runaway spend. |

### Queue and cache

| Variable | Required | Default | Description |
|---|---|---|---|
| `REDIS_URL` | yes | `redis://localhost:6379/0` | Cache, rate limits, streams. |
| `QUEUE_STREAM_NAME` | no | `workflow_tasks` | Redis Stream key. |
| `QUEUE_CONSUMER_GROUP` | no | `workers` | Consumer group name. |
| `QUEUE_VISIBILITY_TIMEOUT_SECONDS` | no | `300` | Before a pending task is reclaimed. |
| `QUEUE_MAX_DELIVERIES` | no | `5` | Deliveries before the dead-letter stream. |
| `WORKER_CONCURRENCY` | no | `8` | Concurrent node tasks per worker process (asyncio semaphore, not threads). |
| `WORKER_REPLICAS` | no | `1` | Worker processes to run. Execution throughput scales here. |

### Execution

| Variable | Required | Default | Description |
|---|---|---|---|
| `HTTP_TIMEOUT_SECONDS` | no | `30` | Per outbound provider call. |
| `HTTP_MAX_RETRIES` | no | `3` | Retries for retryable categories only. |
| `HTTP_BACKOFF_BASE_MS` | no | `250` | Exponential backoff base; jitter is always applied. |
| `CIRCUIT_BREAKER_THRESHOLD` | no | `5` | Consecutive failures before opening per provider. |
| `CIRCUIT_BREAKER_COOLDOWN_SECONDS` | no | `60` | Time before a half-open probe. |
| `EXECUTION_ALLOWLIST_ONLY` | no | `true` | Refuse calls to non-allowlisted providers. **Keep `true`.** |

### Security and credentials

| Variable | Required | Default | Description |
|---|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | yes | — | **Secret.** 32-byte key, base64-encoded, for AES-256-GCM. Generate with `openssl rand -base64 32`. |
| `CREDENTIAL_KEY_VERSION` | no | `1` | Increment when rotating; old rows decrypt with their stored version. |
| `JWT_SIGNING_SECRET` | yes | — | **Secret.** Session tokens. |
| `JWT_TTL_MINUTES` | no | `60` | Session lifetime. |
| `OAUTH_REDIRECT_BASE_URL` | no | `http://localhost:8080` | OAuth callback base. |
| `RATE_LIMIT_PER_USER_PER_MINUTE` | no | `120` | Gateway quota. |
| `SSRF_BLOCK_PRIVATE_RANGES` | no | `true` | Reject private/link-local/loopback egress. **Keep `true`.** |

### Provider credentials (development only)

Real credentials belong in the encrypted credential store via the UI, not in `.env`.
These exist only to make local development of a provider integration convenient, and
are read solely when `APP_ENV=development`.

| Variable | Description |
|---|---|
| `DEV_GITHUB_TOKEN` | **Secret.** Personal access token, minimum scopes. |
| `DEV_SLACK_BOT_TOKEN` | **Secret.** Bot token for a scratch workspace. |
| `DEV_WEBHOOK_PUBLIC_URL` | Public tunnel URL (ngrok or similar) for inbound webhooks. |

### Observability

| Variable | Required | Default | Description |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | `http://localhost:4317` | Collector endpoint. |
| `OTEL_SERVICE_NAME` | yes | — | Per service: `gateway`, `search`, `agent`, `worker`, `ingest`. |
| `OTEL_TRACES_SAMPLER_ARG` | no | `1.0` | Sample rate; lower in production. |
| `METRICS_PORT` | no | `9464` | Prometheus scrape port. |

## 5. Secret handling

- `.env` is gitignored. `.env.example` holds every key with a placeholder value and
  is committed.
- Variables marked **Secret** above never appear in logs, traces, error messages,
  prompts, test fixtures, or commits.
- Production secrets come from the platform's secret manager or a cloud KMS, not
  from a `.env` file.
- Rotating `CREDENTIAL_ENCRYPTION_KEY`: add the new key, increment
  `CREDENTIAL_KEY_VERSION`, re-encrypt stored rows with a migration job, then retire
  the old key. Stored rows record the version they were encrypted under, so this is
  safe to do incrementally.
- If a secret is ever committed, treat it as compromised: revoke it at the provider
  first, then clean history.

## 6. Environment profiles

| Profile | Data | LLM | External calls |
|---|---|---|---|
| `development` | local containers, sample specs | real, small budget | dev tokens, scratch workspaces |
| `test` | ephemeral containers, fixtures | stubbed client | mocked transport only |
| `staging` | production-like, full index | real | allowlisted providers, test accounts |
| `production` | managed services | real | allowlisted providers only |

In `test`, an attempt to make a real outbound call fails the test rather than
falling through to the network — the HTTP transport is replaced, not merely
configured.

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Gateway exits on start with a config error | A required variable is missing; the message names it |
| Search returns nothing after ingestion | Index not built — run `make reindex`; check `OPENSEARCH_INDEX` matches |
| `pgvector` errors on insert | `EMBEDDING_DIMENSIONS` does not match the column; a reindex and migration is needed |
| Workers idle while tasks pile up | Consumer group name mismatch, or the stream key differs between producer and worker |
| One slow provider stalls unrelated executions | A blocking call slipped into an async path — see [conventions.md](conventions.md#7-async-discipline) |
| `ModuleNotFoundError: core` | Run `uv sync` from the repository root; apps import the workspace package, not a copy |
| Every provider call fails with a policy error | Provider not allowlisted, or `EXECUTION_ALLOWLIST_ONLY` is doing its job |
| Webhook never fires locally | `DEV_WEBHOOK_PUBLIC_URL` is stale — tunnels change on restart |
