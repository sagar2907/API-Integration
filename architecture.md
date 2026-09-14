# Architecture

## 1. System overview

```
                        ┌──────────────────────┐
                        │   Web Frontend       │  Next.js (+ BFF)
                        │  search · builder    │
                        │  · dashboard         │
                        └──────────┬───────────┘
                                   │ HTTPS / JSON
                        ┌──────────▼───────────┐
                        │    API Gateway       │  Python · FastAPI
                        │  authn/authz · rate  │
                        │  limits · routing    │
                        └──────────┬───────────┘
              ┌────────────────────┼────────────────────┐
              │                    │                    │
    ┌─────────▼────────┐ ┌─────────▼────────┐ ┌─────────▼────────┐
    │  Agent Layer     │ │ Search & Ranking │ │ Workflow Engine  │
    │  (FastAPI)       │ │ (FastAPI)        │ │ (asyncio worker) │
    ├──────────────────┤ ├──────────────────┤ ├──────────────────┤
    │ Intent parser    │ │ BM25 index       │ │ DAG + state store│
    │ Selection agent  │ │ Vector index     │ │ Compiler         │
    │ Planner          │ │ Hybrid fusion    │ │ Task executor    │
    │ Recovery agent   │ │ Capability filter│ │ Scheduler        │
    └─────────┬────────┘ └─────────┬────────┘ └─────────┬────────┘
              └────────────────────┼────────────────────┘
                                   │
                        ┌──────────▼───────────┐
                        │ Validation / Policy  │  <- the gate
                        │ endpoint existence · │
                        │ schema compat · auth │
                        └──────────┬───────────┘
                        ┌──────────▼───────────┐
                        │ Credential / Secret  │
                        │ Layer (encrypted)    │
                        └──────────┬───────────┘
                        ┌──────────▼───────────┐
                        │    External APIs     │
                        └──────────┬───────────┘
                        ┌──────────▼───────────┐
                        │ Verification + Logs  │
                        └──────────┬───────────┘
                        ┌──────────▼───────────┐
                        │ Metrics / Traces     │  OTel -> Prometheus -> Grafana
                        └──────────────────────┘
```

Supporting stores: **PostgreSQL** (metadata, workflows, executions), **OpenSearch**
(lexical index), **pgvector** (embeddings), **Redis** (cache, rate limits, queue).

## 2. The trust boundary

This is the most important diagram in the project.

```
      UNTRUSTED (model output)          │        TRUSTED (deterministic)
                                        │
  intent JSON, candidate choice,        │   endpoint records, JSON schemas,
  proposed DAG, field mappings,         │   auth schemes, compiled IR,
  natural-language explanations         │   credentials, executed requests
                                        │
                    ──────────►  [ VALIDATOR ]  ──────────►
                                        │
   Anything crossing left-to-right must be re-resolved against the database.
   Nothing crosses right-to-left except redacted metadata.
```

Rules enforced at the boundary:

- **Endpoint resolution** — the plan references `endpoint_id`s. The validator looks
  each one up; unknown or deprecated IDs reject the plan.
- **No free URLs** — the executor constructs URLs from `providers.base_url` plus
  `endpoints.path`. There is no code path that accepts a model-supplied URL.
- **Secret isolation** — credentials are fetched by the executor at call time and
  referenced in plans only by `credential_ref`. The agent service never receives a
  secret value, and secrets never enter a prompt or a log.
- **Egress allowlist** — the HTTP client resolves the host and rejects private and
  link-local address ranges, plus any host not belonging to an allowlisted provider
  (SSRF protection).

## 3. Request paths

### 3.1 Discovery (synchronous, target p95 under 1 s)

```
POST /v1/search
  -> gateway (authn, rate limit)
  -> search service
      |- normalize query (lowercase, tokenize, expand action verbs)
      |- BM25 over OpenSearch          --+
      |- ANN over pgvector             --+--> reciprocal-rank fusion
      |- filter: method, auth support, provider allowlist, deprecated = false
  -> top-K (K = 20) endpoint candidates + scores + why-matched terms
```

Redis caches normalized-query to candidate-ID lists for hot queries. Endpoint
metadata is cached by ID under a version-stamped key, so ingestion invalidates it.

### 3.2 Planning (seconds, LLM in the loop)

```
POST /v1/workflows/plan  { goal: "..." }
  -> gateway -> agent service
      1. Intent parser        -> structured requirement JSON
      2. Retrieval            -> calls search service per requirement slot
      3. Selection agent      -> picks a compatible endpoint set from candidates
      4. Planner              -> proposes DAG + field mappings
      5. Self-check loop      -> run validator; on failure feed errors back (max 3)
  -> returns draft workflow (status = draft, never executable yet)
```

The self-check loop is bounded, and every iteration logs its token cost.

### 3.3 Compilation (deterministic, no LLM)

```
POST /v1/workflows/{id}/compile
  1. Structural    - is it a DAG? single trigger? all nodes reachable?
  2. Existence     - every endpoint_id resolves, is active, method + path match
  3. Parameters    - required path/query/header params have sources
  4. Body          - request body validates against the endpoint's JSON schema
  5. Auth          - provider auth scheme is supported and a credential is bound
  6. Compatibility - per edge, upstream output satisfies downstream required input
  7. Policy        - destructive ops flagged for approval; provider allowlisted
  -> compiled IR (immutable, versioned) or a list of typed validation errors
```

Compilation output is stored, not recomputed at execution time. An execution always
runs one specific compiled IR version, which is what makes runs reproducible.

### 3.4 Execution (asynchronous, durable)

```
POST /v1/workflows/{id}/executions   (or a webhook / schedule trigger)
  -> enqueue (Redis Streams)
  -> worker claims task (consumer group, visibility timeout)
       for each ready node in topological order:
         - load node config from the compiled IR
         - resolve inputs from upstream node outputs (checkpointed)
         - resolve credential, build request from indexed metadata
         - call with timeout, retry with exponential backoff + jitter
         - honor Retry-After; per-provider token bucket in Redis
         - idempotency key = hash(execution_id, node_id, stable payload)
         - persist node result + execution_event BEFORE acknowledging
       -> verification node -> status
  -> repeated failures -> dead-letter stream
```

Checkpoint-then-acknowledge ordering is deliberate: a worker crash after an external
call replays the node, and the idempotency key prevents a duplicate side effect.

## 4. Component responsibilities

| Component | Owns | Explicitly does not |
|---|---|---|
| Gateway | authn, per-user quotas, request shape, routing | business logic |
| Ingest | spec parsing, normalization, dedup, indexing | serving queries |
| Search | lexical + semantic retrieval, fusion, filtering | choosing the endpoint |
| Agent | intent, selection, planning, mapping, explanation, recovery | executing anything |
| Validator | every deterministic check, producing the compiled IR | calling external APIs |
| Executor | HTTP calls, retries, rate limits, checkpoints | deciding what to call |
| Verifier | status/schema checks, side-effect confirmation, error classification | retry policy |
| Credentials | encrypted storage, scoped issuance, audit trail | being visible to the agent |

## 5. Data model

```
providers        provider_id, name, base_url, documentation_url, auth_type,
                 allowlisted, created_at
apis             api_id, provider_id, name, version, description, spec_hash,
                 deprecated_at
endpoints        endpoint_id, api_id, method, path, operation_id, summary,
                 description, tags[], is_deprecated, is_destructive
parameters       parameter_id, endpoint_id, location, name, type, required,
                 description
schemas          schema_id, endpoint_id, direction, status_code, json_schema
auth_schemes     auth_scheme_id, api_id, scheme, scopes[], config
credentials      credential_id, owner_id, provider_id, scheme, ciphertext,
                 key_version, scopes[], expires_at
workflows        workflow_id, owner_id, name, version, status, definition,
                 parent_version, created_at
workflow_nodes   node_id, workflow_id, type, endpoint_id, configuration
compiled_ir      ir_id, workflow_id, workflow_version, ir, compiled_at
executions       execution_id, workflow_id, ir_id, trigger_type, status,
                 started_at, completed_at, idempotency_key
execution_events execution_id, node_id, ts, event_type, attempt, latency_ms,
                 status_code, error_category, payload_metadata
credential_audit audit_id, credential_id, execution_id, node_id, ts, action
```

- `parameters.location` is one of `path`, `query`, `header`, `cookie`.
- `schemas.direction` is `request` or `response`.
- `auth_schemes.scheme` is one of `apikey`, `bearer`, `oauth2`, `basic`.
- Node types: `trigger`, `api_call`, `transform`, `condition`, `validate`, `approval`.

Workflows are versioned by insert, never by update — a new version is a new row, so
an execution always points at the exact definition it ran.

`execution_events` and `credential_audit` are append-only. `payload_metadata` stores
shapes, sizes, and hashes — never raw payload bodies, which may contain user data.

## 6. Failure taxonomy

Every failure is classified into exactly one category, which drives both retry
policy and the recovery agent's context:

| Category | Retryable | Typical cause |
|---|---|---|
| `discovery` | no | no candidate endpoint matched the requirement |
| `validation` | no | schema incompatibility, missing required field |
| `auth` | no | missing or expired credential, insufficient scope |
| `rate_limit` | yes, with backoff | 429, provider quota exhausted |
| `network` | yes | timeout, DNS failure, connection reset |
| `provider` | yes if 5xx | upstream error, malformed response |
| `data_mapping` | no | mapped value failed type or format check at runtime |
| `internal` | yes | our bug — always alerts |

## 7. Runtime and scaling posture

Every backend service is Python. They are separate **processes**, not separate
languages — that is what lets search and execution scale independently.

```
gateway    gunicorn + uvicorn workers   N processes, stateless, behind a load balancer
search     gunicorn + uvicorn workers   N processes, read-only, scales with query load
agent      gunicorn + uvicorn workers   N processes, scales with planning load
worker     plain asyncio process        M replicas, scales with execution throughput
ingest     batch job / CLI              offline, never on a request path
```

Concurrency inside one process comes from `asyncio`: a worker holds many provider
calls in flight on a single event loop, because the workload is I/O bound — it is
waiting on the network, not computing. Parallelism across cores comes from running
more processes. This is why the GIL is not a factor here.

The three CPU-bound pieces of work are kept off the event loop deliberately:
embedding generation and spec parsing run in the ingestion job, and JSON Schema
compilation is cached at compile time rather than repeated per execution. Any future
CPU-bound work goes to a process pool. The discipline that keeps this true — never a
blocking call in an async path — is in
[conventions.md](conventions.md#7-async-discipline).

Rate limits are coordinated in Redis rather than held per process, so adding worker
replicas increases throughput without multiplying the request rate seen by a
provider. Circuit-breaker state is shared for the same reason.

## 8. Decisions and their reasons

- **One backend language.** The Pydantic models the planner emits, the validator
  rejects, and the executor consumes are literally the same classes. A split-language
  backend would define those contracts twice and let them drift; this is the single
  biggest correctness lever in a system whose thesis is deterministic validation.
- **Hybrid retrieval, not embeddings alone.** API queries are full of exact tokens
  (`repos`, `POST`, `chat.postMessage`) where lexical matching is strong; semantics
  cover paraphrase. Fusing both beats either alone, and BM25 gives an explainable,
  cheap, low-latency baseline to measure against.
- **Compile to an IR.** Separating planning from execution makes runs reproducible,
  lets validation be a hard gate, and keeps the LLM out of the hot path.
- **Queue-based execution.** External APIs are slow and flaky; synchronous execution
  couples user latency to provider latency and loses work on crashes.
- **A hand-written consumer rather than a task framework.** Celery or arq would own
  the acknowledgement lifecycle, and acknowledgement ordering is exactly the property
  this system must control (see 3.4). Redis Streams supply the primitives; the loop
  is ours.
- **Checkpoint before acknowledge.** At-least-once delivery plus idempotency keys is
  simpler and more honest than pretending to have exactly-once semantics.
- **Append-only audit.** Security review and post-mortems both need a record that
  cannot be rewritten by the code path under investigation.
