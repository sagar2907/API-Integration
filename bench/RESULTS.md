# Retrieval Benchmark Results

*Measured 2026-08-28. Regenerate with `uv run python bench/run_bench.py`.*

These numbers come from an actual run against the full corpus. Nothing here
is a target or an estimate.

## Setup

- **Corpus**: 118 providers / 10563 endpoints (APIs.guru snapshot)
- **Indexed endpoints**: 15,504
- **Queries**: 30 hand-labelled, binary relevance
- **Embedding model**: local/BAAI/bge-small-en-v1.5 (384d)
- **Retrieval depth**: top 20
- **Synonym expansion**: True

## Results

| Mode | P@5 | R@20 | MRR | NDCG@10 | p50 latency | p95 latency | queries with no hit |
|---|---|---|---|---|---|---|---|
| **bm25** | 0.133 | 0.489 | 0.372 | 0.295 | 16 ms | 32 ms | 8 |
| **vector** | 0.153 | 0.578 | 0.409 | 0.324 | 376 ms | 468 ms | 6 |
| **hybrid** | 0.207 | 0.598 | 0.485 | 0.394 | 458 ms | 505 ms | 6 |

### How to read these

- **P@5** — of the top 5 results, what fraction were correct. Divided by 5
  even when a query has only one correct answer, so 0.2 is the ceiling for
  single-answer queries. Compare modes to each other, not to 1.0.
- **R@20** — of all correct answers, what fraction appeared in the top 20.
  This is the number that matters most here: the planner receives the top 20,
  so anything outside it is invisible to the rest of the system.
- **MRR** — average of 1/(rank of first correct answer). 1.0 means every
  query was answered at rank 1.
- **NDCG@10** — ranking quality with a logarithmic discount for lower
  positions, normalised so queries with different answer counts compare fairly.

Synonym expansion was **on** for this run. Toggle `SEARCH_EXPAND_SYNONYMS` in `.env` and re-run to compare.

## Experiments run against this query set

### 1. Synonym expansion — keep it

Day 2 left this open. A single probe query suggested expansion made ranking
worse; measured across all 30 queries it clearly helps, which is exactly why
the decision was deferred to a benchmark instead of settled by anecdote.

| Mode | NDCG@10 off | NDCG@10 on | R@20 off | R@20 on | misses off | misses on |
|---|---|---|---|---|---|---|
| bm25 | 0.248 | **0.301** | 0.451 | **0.517** | 10 | **8** |
| hybrid | 0.383 | **0.412** | 0.607 | 0.601 | 6 | 6 |

Vector retrieval is unaffected, as expected — it never touches the lexical
match expression. Expansion costs about 3 ms of BM25 latency.

**Decision: expansion stays on.**

### 2. Symmetric synonyms — rejected

The synonym table declares `issue -> bug` without `bug -> issue`. Synonymy is
symmetric by definition, so deriving the reverse direction automatically looks
like an obvious correctness fix. Measured, it was worse:

| | asymmetric (kept) | symmetric (rejected) |
|---|---|---|
| bm25 NDCG@10 | **0.301** | 0.241 |
| bm25 R@20 | **0.517** | 0.451 |
| bm25 queries with no hit | **8** | 11 |
| hybrid NDCG@10 | **0.412** | 0.384 |

It fixed one query (`open a bug report`) and broke five. Expansion is not free:
every additional term dilutes the signal, and symmetrising produced cascades
like `user -> member -> account`. A theoretically correct change that the data
rejected — reverted, and documented in `search/text.py` so it is not retried.

## Known weaknesses

Three queries return nothing correct in *any* mode:

| Query | Why it fails |
|---|---|
| `open a bug report in a code repository` | Providers say "issue"; the query says "bug report" |
| `schedule a video conference call` | Zoom says "meeting"; nothing says "video conference" |
| `propose code changes for review` | Everyone says "pull request" |

These were written as deliberate vocabulary traps, and embeddings did not
bridge them either — a useful corrective to the assumption that semantic
search solves vocabulary mismatch in general. Fixing them by adding synonyms
aimed at these specific queries would be fitting the test set; the honest
options are a larger query set, a reranking stage, or a stronger embedding
model, all measured against fresh labels.

## Latency and the 500 ms target

The specification targets sub-500 ms discovery. BM25 clears it by a factor of
50. Hybrid sits at roughly 450 ms p50 and **just breaches 500 ms at p95** —
almost all of it the ~350 ms spent embedding the query locally, not the search
itself. Real options if that matters: cache query embeddings, use a smaller
model, or accept BM25 for interactive search and reserve hybrid for planning,
where an extra 400 ms is irrelevant next to an LLM call.

## Rank of the first correct answer, per query

**1** means the top result was correct. — means no correct answer in the top 20.

| Query | bm25 | vector | hybrid |
|---|---|---|---|
| q01 post a message to a chat channel | **1** | **1** | **1** |
| q02 open a bug report in a code repository | — | — | — |
| q03 take a payment from a credit card | 2 | 7 | **1** |
| q04 send an email message to a recipient | 8 | — | — |
| q05 schedule a video conference call | — | — | — |
| q06 upload a file to cloud storage | 6 | 11 | 2 |
| q07 register a callback url to receive event not | 5 | 4 | 3 |
| q08 list the code repositories belonging to a us | 8 | 8 | 3 |
| q09 add a new customer record | 2 | 2 | **1** |
| q10 refund money back to a customer | 2 | **1** | 2 |
| q11 create a new user account | 2 | **1** | 2 |
| q12 list the chat channels in a workspace | 13 | **1** | 3 |
| q13 invite a person to a conversation | **1** | 10 | **1** |
| q14 permanently delete a file | **1** | **1** | **1** |
| q15 propose code changes for review | — | — | — |
| q16 start a recurring billing subscription | 6 | **1** | **1** |
| q17 search for people by username | — | 12 | — |
| q18 check the available account balance | **1** | **1** | **1** |
| q19 send a text message to a phone number | — | 8 | 13 |
| q20 create a to-do task | 8 | 3 | 2 |
| q21 add a card to a kanban board | — | — | 16 |
| q22 view the commit history of a repository | — | 8 | 20 |
| q23 create a new branch in version control | 11 | 2 | **1** |
| q24 get the profile details of a user | — | 18 | — |
| q25 create a new code repository | 2 | — | 3 |
| q26 generate an invoice for a customer | 13 | 11 | 5 |
| q27 list the open issues in a repository | **1** | 2 | 2 |
| q28 add a label to an issue | **1** | **1** | **1** |
| q29 list the members of a team | **1** | **1** | **1** |
| q30 upload a video | 2 | 4 | 3 |
