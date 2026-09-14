# Conventions

House rules for code in this repository. The backend is Python end to end; the
frontend is Next.js end to end. When something here conflicts with a language's own
idiom, the language idiom wins — say so in review.

## 1. Non-negotiables

1. **No model-supplied URL, host, method, or path ever reaches the HTTP client.**
   Requests are built from indexed endpoint records. There is no escape hatch.
2. **No secret value in a prompt, a log line, an error message, a trace attribute,
   a test fixture, or a commit.** Credentials are referenced by ID until the moment
   of use.
3. **No execution without a compiled IR.** If it did not pass the validator, it does
   not run.
4. **No raw request/response bodies in persistent logs.** Store shape, size, hash,
   and status; that is enough to debug and safe to keep.
5. **No blocking call in an async path.** See [§7](#7-async-discipline).

Reviewers: a change violating any of these is rejected regardless of how well it
otherwise reads.

## 2. Naming

- **Files and directories**: `snake_case` for Python modules, lowercase with hyphens
  for non-Python directories, `PascalCase.tsx` for React components.
- **Database**: `snake_case`, plural tables (`endpoints`), singular columns, IDs as
  `<entity>_id`, timestamps as `<verb>_at` (`created_at`, `completed_at`).
- **JSON over the wire**: `snake_case` keys, matching the database and the Pydantic
  models, so no translation layer is needed. The frontend consumes `snake_case` and
  does not rename fields on the way in.
- **Python**: `snake_case` functions and variables, `PascalCase` classes,
  `UPPER_SNAKE` constants. Private helpers get a single leading underscore. Modules
  are nouns (`ranking.py`, `compiler.py`), not `utils.py`.
- **TypeScript**: `camelCase` locals, `PascalCase` types and components. Types for
  gateway responses are zod schemas, never hand-written interfaces that can drift.
- Prefer names that state the domain concept: `candidate_endpoints`, not `results`;
  `compiled_ir`, not `data`.

## 3. Types and models

- **`packages/core/models/` is the single source of truth.** Every cross-service
  contract — requirement JSON, plan, compiled IR, validation error, execution event
  — is a Pydantic v2 model defined once and imported by every app. If two services
  disagree about a shape, that is a bug in this rule, not a reason to add a
  converter.
- `mypy --strict` passes. No bare `Any`, no unjustified `# type: ignore` — a needed
  one carries a comment saying why.
- Use `model_config = ConfigDict(extra="forbid")` on models parsed from LLM output.
  Silent extra fields are how hallucinated structure sneaks through.
- **Pydantic validates our contracts; `jsonschema` validates provider payloads**
  against schemas taken from OpenAPI specs. Do not use one for the other's job.
- SQLAlchemy models live in `packages/core/db/` and never leave it — services
  exchange Pydantic models, so persistence details stay swappable.

## 4. Errors

A small hierarchy rooted at `EngineError`, in `packages/core/errors.py`:

```python
class EngineError(Exception):
    category: ErrorCategory          # from the failure taxonomy
    retryable: bool

class EndpointNotFoundError(EngineError): ...
class SchemaIncompatibleError(EngineError): ...
class CredentialMissingError(EngineError): ...
class ProviderError(EngineError): ...
```

Rules:

- **Every error carries a category** from the failure taxonomy in
  [architecture.md](architecture.md#6-failure-taxonomy). Not optional — the metrics
  and the recovery agent both key off it.
- Never a bare `except:` or a bare `except Exception:` without re-raising or
  classifying. Catching to log and swallow is prohibited outside the worker's
  top-level loop.
- Chain causes: `raise SchemaIncompatibleError(...) from err`. The traceback is the
  debugging artifact.
- **Validation errors are structured, not strings:**

```json
{
  "code": "schema_incompatible",
  "node_id": "n3",
  "field": "message.text",
  "expected": "string",
  "actual": "object",
  "hint": "map issue.title into message.text"
}
```

The frontend renders these, and the planner's self-check loop consumes them. A
free-text error is a dead end for both.

## 5. Logging

`structlog` with JSON output. Required fields on every line: `ts`, `level`,
`service`, `trace_id`, `event`. Add `execution_id`, `node_id`, `workflow_id`,
`error_category` wherever they exist — bind them to the context once per task rather
than passing them to each call.

Levels: `error` = needs a human; `warn` = degraded but handled (retry, fallback);
`info` = state transitions worth keeping; `debug` = development only, off in prod.

Never log: credentials, tokens, `Authorization` headers, request bodies, response
bodies, user PII. Redaction is a structlog processor in `packages/core/telemetry.py`
— built once, applied to everything. Do not redact at call sites.

## 6. Configuration

One `Settings` class per app using `pydantic-settings`, instantiated at startup and
injected as a FastAPI dependency. A missing required variable is a startup crash
with a message naming it, never a `None` discovered at request time. Never read
`os.environ` outside the settings module. Every new variable is documented in
[env.md](env.md) and added to `.env.example` in the same commit.

No magic numbers — timeouts, retry counts, `K`, backoff bases, and cache TTLs are
named constants or settings fields.

## 7. Async discipline

This is the discipline that makes one Python backend viable for an execution engine.

- **Every I/O call is `await`ed against an async client**: `httpx.AsyncClient`,
  SQLAlchemy async sessions, `redis.asyncio`, `opensearch-py` async. A synchronous
  driver in an async path stalls every other in-flight task on that process.
- **Never call `time.sleep`, `requests`, blocking file I/O, or a sync DB driver in
  an `async def`.** Backoff uses `asyncio.sleep`.
- CPU-bound work — embedding, large spec parsing, heavy JSON Schema compilation —
  runs in the ingest job, a process pool, or at compile time. If it must happen
  inline, wrap it in `asyncio.to_thread` and say why in a comment.
- Bound concurrency explicitly: `asyncio.Semaphore` per provider, plus the shared
  Redis token bucket. Unbounded `gather` over candidates is how you get rate-limited.
- Propagate cancellation. Never swallow `asyncio.CancelledError`; on shutdown,
  finish or checkpoint in-flight nodes, then exit. Graceful shutdown is what makes
  redeploys safe mid-execution.
- Every `await` on a network call has a timeout. No exceptions.
- Clients are created once per process (lifespan startup), never per request —
  connection pooling is the whole point.

## 8. Tests

- **Unit tests are mandatory for deterministic components**: intent schema
  validation, query normalization, ranking fusion, schema compatibility, the
  compiler, retry/backoff logic, the credential layer, error classification. These
  run with no network and no LLM.
- `pytest.mark.parametrize` for case tables — one test function, many cases.
- **Golden-file tests for the ingestion pipeline**: a fixture OpenAPI spec in,
  expected normalized records out. This is how spec-parser regressions get caught.
- **`respx` for every outbound HTTP test.** In the `test` profile the real transport
  is unavailable, so an accidental live call fails loudly instead of quietly working
  on your machine and failing in CI.
- **`hypothesis` property tests** for schema compatibility and field mapping: a
  mapping the compiler accepts must never fail type checking at execution time.
- **Fault-injection tests** for the execution engine: kill a worker mid-node, return
  429 with `Retry-After`, time out, return a malformed body. Assert no duplicate side
  effect and correct resumption.
- **`testcontainers`** for integration tests needing real Postgres, Redis, or
  OpenSearch. Keep them in `tests/integration/` and out of the fast default run.
- **LLM-dependent behavior is tested against a stubbed client** with fixed responses.
  Real-model evaluation belongs in `/bench`, not in the test suite.
- Test names state the behavior: `test_compiler_rejects_deprecated_endpoint`, not
  `test_compiler_2`.

## 9. Code shape

- Functions do one thing; if you need "and" to describe it, split it.
- Dependencies are injected via FastAPI's `Depends` or plain constructor arguments —
  never imported as a module-level singleton and reached for. Tests substitute fakes.
- **Keep I/O at the edges.** Parsing, ranking, matching, and compiling are pure
  functions over data structures. This is what makes the deterministic core testable,
  and it is the difference between a demo and a system.
- No global mutable state. Ever.
- Comment *why*, not *what*. The exception: any non-obvious protocol or security
  decision gets a comment explaining the reasoning, because the next reader will
  otherwise "simplify" it away.

## 10. API design

- Versioned paths: `/v1/...`.
- Plural resources: `/v1/workflows/{id}/executions`.
- POST for state changes, idempotent where a client might retry — accept an
  `Idempotency-Key` header on execution creation.
- Errors return a consistent envelope:

```json
{ "error": { "code": "validation_failed", "message": "...", "details": [ ... ] } }
```

- Pagination is cursor-based (`cursor`, `limit`), never offset, on anything that can
  grow: endpoints, executions, events.
- Long operations return `202` with a resource to poll; nothing blocks on a provider.
- Response models are declared with `response_model=` so FastAPI validates what we
  emit, not merely what we accept.

## 11. Frontend

- Server components fetch data; client components handle interaction. The workflow
  builder is a client island, the search and dashboard pages are not.
- **Route handlers in `/web/app/api/` are a thin BFF only**: attach the session
  cookie, proxy to the gateway, return the response. No business logic, no direct
  database access, no provider calls.
- Every gateway response is parsed through a zod schema at the boundary. A shape
  change surfaces as a parse error at one place, not as `undefined` three components
  deep.
- Server state belongs to TanStack Query — no duplicating it into React state.
- The browser never holds a gateway token or a provider credential; the session
  cookie is `httpOnly`.

## 12. Git

- Branches: `phase-2/bm25-search`, `fix/retry-jitter`, `docs/architecture`.
- Conventional-commit subjects: `feat(search): add reciprocal-rank fusion`,
  `fix(worker): honor Retry-After on 429`, `docs(env): document KMS variables`.
- Subject in the imperative mood, 72 characters or fewer. Body explains why.
- One concern per commit; a green test suite at every commit on the main branch.
- Never commit `.env`, credentials, dumps, or vendor spec archives over 10 MB.

## 13. Documentation

- A package gets a `README.md` when its behavior is not obvious from its tests.
- Update the docs in the same commit as the behavior change. A doc that lies is
  worse than no doc.
- Numbers in documentation are labeled either **target** or **measured**, and
  "measured" requires a benchmark run and a date.
