# Autonomous API Discovery & Integration Engine

Turn a plain-English automation goal into a **validated, executable API workflow**.

> *"When a new GitHub issue is created, send a Slack message with the issue title
> and author."*

The system searches an indexed knowledge base of real OpenAPI specifications, has a
language model propose how to connect the endpoints it found, then **verifies every
claim in that proposal against the indexed metadata before anything is allowed to
run** — and executes it with retries, idempotency, and a full audit trail.

**The governing rule:** *LLMs propose; deterministic systems validate and execute.*
The model may suggest an endpoint. It may never invent one.

---

## What it does, in one run

```
$ uv run engine plan "When a new GitHub issue is created,
                      send a Slack message with the issue title and author"

intent    : trigger='a new issue is created in a repository'
            actions=['post a message to a channel']
candidates: 16 endpoints retrieved
attempts  : 1   tokens: 2582

COMPILED  order: n1 -> n2
  n1 [trigger]  POST https://api.github.com/repos/{owner}/{repo}/issues
  n2 [api_call] POST https://slack.com/api/chat.postMessage
      body.channel <- '#general'
      body.text    <- n1.title
```

**The plan never contained a URL.** It names only an endpoint identifier; the
address, method and auth scheme are filled in from the database afterwards. There is
no code path by which a model-written address reaches the network.

---

## Measured results

Everything below came from an actual run. Nothing here is a target.

### Knowledge base

| | |
|---|---|
| APIs indexed | **270** across 120 providers |
| Endpoints | **15,504** |
| Specifications that failed to parse | **1 of 165** (logged and skipped) |
| Re-ingestion | idempotent — identical counts, no duplicates |

### Retrieval — 30 hand-labelled queries, [full results](bench/RESULTS.md)

| Mode | P@5 | R@20 | MRR | NDCG@10 | p50 | p95 |
|---|---|---|---|---|---|---|
| bm25 | 0.147 | 0.517 | 0.366 | 0.301 | **11 ms** | 19 ms |
| vector | 0.187 | 0.601 | 0.430 | 0.358 | 388 ms | 434 ms |
| **hybrid** | **0.207** | **0.601** | **0.505** | **0.412** | 453 ms | 510 ms |

Hybrid wins every quality metric — 37% better NDCG@10 than BM25 alone. It also
*misses* the sub-500 ms target at p95, which is recorded rather than hidden.

### Planning and execution

| | |
|---|---|
| Sentence to compiled workflow | ~20 s, ~2,600 tokens, accepted on the first attempt |
| Crash recovery | re-running a completed execution repeats **no** HTTP call |
| Tests | **245**, all green, no network access |

---

## Quick start

No Docker, no external services — the whole backend runs on SQLite.

```bash
uv sync --extra dev
cp .env.example .env              # add LLM_API_KEY for planning

uv run engine fetch --limit 160   # download OpenAPI specs from APIs.guru
uv run engine ingest              # parse them into the database
uv run engine index               # build the keyword index
uv run engine embed               # build semantic vectors (runs locally, ~45 min)
uv run engine stats
```

Then search, plan, and run:

```bash
uv run engine search "send a message to a channel" --mode hybrid
uv run engine plan "when a github issue opens, post it to slack"
uv run engine credential add slack_com --secret <token>
uv run engine run <workflow_id> --dry-run
uv run engine executions
```

And the web interface:

```bash
uv run uvicorn engine.api.main:app --port 8000   # API + docs at /docs
cd web && npm install && npm run dev             # http://localhost:3000
```

---

## Architecture

```
natural language goal
        ↓
   intent parsing          LLM → a structured requirement
        ↓
   endpoint discovery      BM25 + embeddings over 15,504 endpoints, rank-fused
        ↓
   workflow planning       LLM, restricted to endpoint IDs the search returned
        ↓
 ✱ deterministic validation    seven checks — the gate
        ↓
   compilation             immutable, versioned intermediate representation
        ↓
   execution               retries, backoff, idempotency, checkpoints
        ↓
   verification + audit
```

### The two guards

**The candidate allowlist.** The model may only reference endpoint IDs that *this
request's searches returned*. A real endpoint that exists in the database but was
not retrieved for this goal is still refused. Identifiers are opaque hashes
(`ep_18da3450e690`), so a convincing fake cannot be constructed.

**The compiler.** Seven checks, none of which consult a model:

1. Structure — one trigger, acyclic, every node reachable
2. Existence — every `endpoint_id` resolves to a live row
3. Agreement — method and path match the record, not the plan's claim
4. Parameters — every required path/query/header parameter has a source
5. Body — every required request-body field has a source
6. Compatibility — upstream output actually fits downstream input
7. Policy — auth supported, provider allowlisted, destructive actions gated

Rejections are structured objects, not strings, so the same payload drives both the
UI and the planner's repair loop.

---

## Implemented vs. planned

The design documents describe the mature system. This is a 7-day build; here is
exactly what exists.

| Capability | Status |
|---|---|
| OpenAPI 3 + Swagger 2 ingestion, `$ref` cycles, broken-spec isolation | ✅ built |
| BM25 retrieval with path tokenization and field weighting | ✅ built |
| Semantic retrieval + reciprocal-rank fusion | ✅ built |
| Benchmark suite with hand-labelled queries | ✅ built |
| Workflow DAG model, insert-only versioning | ✅ built |
| Seven-check deterministic compiler | ✅ built |
| JSON-Schema compatibility with documented limits | ✅ built |
| LLM intent parsing and planning, bounded repair loop | ✅ built |
| Encrypted credentials, use-time decryption, audit trail | ✅ built |
| Retries, backoff, `Retry-After`, failure taxonomy | ✅ built |
| Idempotency keys, checkpoints, crash-safe resumption | ✅ built |
| SSRF guard, provider allowlist, dry-run mode | ✅ built |
| Job queue with leases and dead-lettering | ✅ built |
| Web interface: search, create, run history | ✅ built |
| — | |
| PostgreSQL, OpenSearch, pgvector, Redis | ⬜ SQLite stands in for all four |
| Separate service processes | ⬜ one API app plus one worker |
| Real webhook triggers and schedules | ⬜ manual and API-triggered only |
| OAuth 2.0 authorization flow | ⬜ pre-issued tokens only |
| Visual drag-and-drop workflow editor | ⬜ read-only plan view |
| OpenTelemetry, Prometheus, Grafana | ⬜ structured logs + event table |
| Recovery agent, circuit breakers | ⬜ not started |
| 5,000 APIs / 50,000 endpoints | ⬜ 270 / 15,504 |

---

## Known limitations

Stated plainly, because a system like this is only trustworthy if its limits are.

**Validation proves an automation *can* run, never that it does what you meant.**
`title`, `body` and `user.login` are all strings; no checker knows which one was in
your head. In our own demo the model mapped the issue *body* where we asked for the
*title* — a perfectly valid workflow that was not quite the request. This is why
plans are reviewable before being switched on.

**The schema checker does not model everything.** `oneOf`, `anyOf`, `allOf`,
cross-document `$ref`, `format` and numeric ranges are out of scope. Those cases
return *unknown* and are allowed through — a gate against provably wrong plans, not
a proof of correctness. Refusing everything unmodellable would reject most real
specifications.

**Three benchmark queries fail in every retrieval mode**, all vocabulary mismatches
(`"open a bug report"` when every provider says *issue*). Embeddings did not bridge
them either. They are deliberately left unfixed, because adding synonyms aimed at
the test set inflates the score without improving the system.

**Hybrid search exceeds the 500 ms target at p95** (510 ms), almost entirely local
query-embedding time rather than search itself.

**The retrieval benchmark predates the corpus expansion.** The published figures were
measured against 10,563 endpoints; the corpus is now 15,504. They will be re-measured
rather than quietly carried over — a larger corpus generally makes retrieval harder,
so the honest expectation is that some numbers get worse.

---

## Documentation

| File | Contents |
|---|---|
| **[docs/REPORT.md](docs/REPORT.md)** | **Full engineering report** — concepts from scratch, every design decision, and the ones that were wrong. Renders to PDF with `uv run python docs/render_report.py` |
| **[EXPLAINED.md](EXPLAINED.md)** | **Start here if you are new** — the whole project in plain language |
| [architecture.md](architecture.md) | Components, trust boundary, data model, failure taxonomy |
| [techstack.md](techstack.md) | Every technology choice and why |
| [conventions.md](conventions.md) | Coding rules and non-negotiables |
| [env.md](env.md) | Setup and configuration reference |
| [bench/RESULTS.md](bench/RESULTS.md) | Measured retrieval results |
| [todo.md](todo.md) | The full nine-phase roadmap |

## License

MIT
