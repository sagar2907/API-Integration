# The Project, Explained Simply

**A beginner's guide to what we are building, why, and everything done so far.**

This document assumes you know nothing about search engines, APIs, or databases.
Every technical word is explained the first time it appears. It is updated at the
end of every working day, so it always reflects the current state of the project.

*Last updated: after the build — three faults found by the first real prompt.*

---

# Part 1 — What are we building?

## 1.1 The story

Imagine you work at a company. Your manager says:

> "When someone reports a bug on GitHub, can you make it show up in our team's
> Slack channel? I keep missing them."

That sounds like a five-minute job. It is not. Here is what actually happens.

**You have to find out how GitHub can tell you about new bugs.** You search GitHub's
documentation and learn about "webhooks" — a way for GitHub to send a message to a
web address you own whenever something happens. So now you need a web address, and a
small program running at it. That is an hour gone before you have written anything
useful.

**You have to figure out what GitHub actually sends you.** It is a blob of data about
200 lines long. The bug's title is where you'd guess: `issue.title`. The person who
reported it is *not* where you'd guess — it is not `issue.author`, it is
`issue.user.login`. There is no way to know that except by reading very carefully.
This is where most people lose half an hour.

**You have to do the whole thing again for Slack.** Create a "Slack app", pick
permissions, install it, copy out a secret token, and discover your bot cannot post
to the channel until somebody invites it.

**You have to translate between them.** GitHub gives you a `title`. Slack wants a
`text`. You write code to move one into the other.

**Then you hit the traps.** Slack often replies "200 OK" — the code that normally
means success — while hiding the real failure inside the response body. So the
obvious check ("did I get a 200? great") is wrong, and your automation reports
success while silently doing nothing.

**Then the questions you didn't think about.** What if Slack is down for 20 seconds?
What if your program crashes right after sending, and on restart sends the message a
second time? What if it quietly breaks and nobody notices for a week?

**Honest total: one to two days.** For something you described in one sentence. And
what you built is throwaway code that does exactly this one thing.

## 1.2 Now multiply it

That was *one* automation between *two* tools. A normal company runs thirty to fifty:
payment failed → email the customer; new employee hired → create their accounts in
six systems; support ticket marked urgent → page the on-call engineer.

Every one has the same shape — *something happens over here, so do something over
there, carrying some data across* — but the details differ every time. Different
documentation, different login method, different field names. You cannot copy-paste
your GitHub-to-Slack code into a Stripe-to-QuickBooks job.

**That is the problem this project solves.** Not that the work is hard — that it is
tedious, endlessly repeated, and just different enough each time to resist reuse.

## 1.3 Why existing solutions aren't enough

**Writing it yourself** gives total control and costs days each time.

**Zapier / Make / n8n** are the obvious answer, and genuinely good. But a human at
that company hand-built every single connector. If your tool isn't in their catalogue
you're stuck. If it *is* but the specific operation you need wasn't one they exposed,
you're stuck. Internal company tools are never in the catalogue.

**Asking ChatGPT to write the code** is fast and flexible, and has one disqualifying
flaw: **it confidently makes things up.** It will tell you the field is `issue.author`
because that is what a sensible API *would* call it. It will invent an endpoint that
sounds exactly right and does not exist. It states the wrong things with exactly the
same confidence as the right ones, and you find out at runtime.

## 1.4 The gap we fill

We want ChatGPT's flexibility — describe any automation in plain English — with
Zapier's reliability, and with made-up answers made *impossible* rather than merely
unlikely.

**Nothing in the middle exists. That is this project.**

---

# Part 2 — The words you'll keep seeing

Read this once; everything later will make sense.

| Word | What it actually means |
|---|---|
| **API** | A way for one program to talk to another. Slack's API is how your code asks Slack to post a message. |
| **Endpoint** | One specific action an API offers. `POST /chat.postMessage` is Slack's "send a message" endpoint. An API has hundreds. |
| **HTTP method** | The verb of the action. `GET` = fetch something, `POST` = create something, `DELETE` = remove something. |
| **Path** | The address of the endpoint, like `/repos/{owner}/{repo}/issues`. The `{curly braces}` are blanks you fill in. |
| **Schema** | A description of the *shape* of data: which fields exist, what type each is (text, number, true/false), and which are mandatory. |
| **JSON** | The standard text format for sending structured data between programs. Looks like `{"title": "Bug in login"}`. |
| **OpenAPI spec** | A machine-readable manual for an entire API — every endpoint, every field, every login requirement, in one file. **This is the raw material our whole project runs on.** |
| **Authentication (auth)** | Proving who you are, usually with a secret key or token, so the API lets you in. |
| **Webhook** | The reverse of a normal API call: instead of you asking them, they notify you when something happens. |
| **Workflow / DAG** | The plan of an automation: a series of steps with arrows between them. DAG means the arrows never loop back on themselves. |
| **Validation** | Checking that something is correct *before* running it. The heart of this project. |
| **LLM** | Large Language Model — ChatGPT, Gemini, Claude. The AI that reads English and writes structured output. |
| **Embedding** | A list of numbers representing the *meaning* of a piece of text. Similar meanings produce similar numbers. |
| **BM25** | A classic, non-AI algorithm for ranking search results by keyword matching. Decades old and still excellent. |
| **Index** | A pre-built lookup structure that makes searching fast — like the index at the back of a textbook. |

---

# Part 3 — How the whole system works

## 3.1 The one rule everything hangs on

> **The AI is allowed to suggest. It is never allowed to act on its own word.**

An analogy. Imagine a very fast, very confident intern who has read a lot of
documentation *from memory*. Ask them anything and you get an instant, plausible,
authoritative answer — sometimes wrong, and they sound identical either way. That is
ChatGPT writing integration code.

Now imagine the same intern, but with the actual reference manual open on the desk,
and a supervisor who checks every single claim against that manual before it goes
anywhere near production. Same speed on the suggestions. Zero fabrications reaching
reality.

**That is our system.** The AI proposes; a dumb, strict, non-AI checker verifies every
claim against a database of real API documentation; only then does anything run.

And notice *why* the checking is possible at all: the supervisor isn't judging whether
the code is elegant. They are answering mechanical questions — does this endpoint
exist in the manual? Is this field text or a number? Those have definite answers.

## 3.2 The pipeline

```
   You type: "when a GitHub issue is created, Slack me the title and author"
                              |
   1. UNDERSTAND      AI turns your sentence into a structured checklist
                              |
   2. SEARCH          Find matching endpoints among 10,563 we have indexed
                              |
   3. PLAN            AI proposes: use this endpoint, then that one, map this
                      field to that field
                              |
   4. VALIDATE  ★     Non-AI checker verifies EVERY claim against the database.
                      Anything false = rejected. This is the gate.
                              |
   5. COMPILE         Turn the approved plan into a fixed, runnable form
                              |
   6. RUN             Actually call the APIs, with retries and safety measures
                              |
   7. VERIFY          Confirm it really worked; record everything that happened
```

Steps 1 and 3 are the AI. Step 4 is the gate. Steps 5–7 are ordinary careful
engineering. **Roughly 80% of the work is in the non-AI parts** — which is exactly
what makes this a real engineering project rather than a chatbot with a nice screen.

## 3.3 An important clarification: this is not a GitHub/Slack tool

GitHub and Slack are **2 of the 118 API providers** currently in the database. They
account for about 1,000 of 10,563 endpoints — under 10%. The other 90% includes
Stripe, Jira, Zoom, Trello, Bitbucket, Box, Asana, AWS Cost Explorer, and about 110
more.

Nowhere in the code is "GitHub" or "Slack" written as logic. They are rows in a
table. We use them for demos only because they are free to test and instantly
understandable.

**Adding a new API is not programming — it is loading a file.** That is the whole
point, and it is what separates this from Zapier's hand-built connectors.

---

# Part 4 — What we have built so far

## Day 1 — The knowledge base

**Goal: get real API documentation into a database we can search.**

### What is the raw material?

Most companies publish an **OpenAPI spec** — one file listing every operation their
API offers, every field, every data type, and how to log in. It exists precisely so
that *programs*, not just humans, can understand an API.

There is a free public project called **APIs.guru** that collects thousands of these
specs in one place. That single fact is what makes this project achievable in a week
instead of a year — we did not write 118 integrations, we downloaded 118 manuals.

### What we built

**1. A downloader** (`src/engine/ingest/fetch.py`)
Fetches the APIs.guru catalogue and downloads spec files, saving them to `data/specs/`
so we never download the same thing twice.

**2. A parser** (`src/engine/ingest/parse.py`) — the hardest part
Reads a spec file and pulls out the useful information in a consistent shape.

This is difficult because real specs are messy:
- There are **two different formats** (OpenAPI 3 and the older Swagger 2), which
  disagree about nearly everything — where the web address lives, how the request
  body is described, how login is declared. We handle both.
- Specs use **references** — "this field looks like the thing defined over there" —
  to avoid repetition. We follow those references and paste the real content in.
- Some references are **circular**: a comment contains replies, and each reply is a
  comment. Following those naively loops forever. We detect the loop and stop.
- Some specs are **broken**. One bad file must never kill the whole run, so every
  spec is processed in a way that logs the failure and moves on.

**3. A storage layer** (`src/engine/db.py`)
Seven tables holding providers, APIs, endpoints, parameters, schemas, login methods,
and a log of each import run.

**4. Identifiers that cannot be guessed** (`src/engine/ids.py`)
Every endpoint gets an ID like `ep_18da3450e690`, produced by scrambling its details
through a mathematical function.

Two reasons, and the second is subtle but important:
- Re-importing the same spec produces the *same* IDs, so we update rows instead of
  creating duplicates. (Verified: running the import twice gives identical counts.)
- Later, the AI will refer to endpoints **by ID**. If IDs looked like
  `github.create_issue`, the AI could plausibly invent `github.delete_repo`. It
  cannot invent `ep_18da3450e690`. **The ID format itself is part of the
  anti-hallucination defence.**

### Results

| | |
|---|---|
| API providers | **118** |
| Endpoints | **10,563** |
| Specs that failed | **0** |
| Flagged as dangerous (delete/remove operations) | 1,282 |

### Three real bugs we hit

**1. The database was 12 copies of GitHub.**
GitHub publishes about 20 near-identical specs (the public one, plus one for every
version of GitHub Enterprise, each ~6 MB). Our "download important providers first"
rule grabbed all of them, so a corpus of 12 specs was 12 GitHubs.
*Fix:* keep only one spec per provider, preferring the main one.

**2. We accidentally cut off the endpoint we needed most.**
To stop one giant API flooding the database, we capped it at 400 endpoints per API.
But GitHub has 845, and `/repos/{owner}/{repo}/issues` — our demo target — sat past
the cut. *Fix:* raised the cap to 1,200.

**3. Downloading took 15 minutes for 4 files.**
We downloaded one at a time, so a few huge files blocked everything behind them.
*Fix:* download 8 at once, and abandon any file over 8 MB.
**Result: 118 specs in ~40 seconds instead of 15 minutes for 4.**

---

## Day 2 — Search

**Goal: type an English phrase, get the right endpoints back out of 10,563.**

After Day 1 we had a filing cabinet with no index. You could look up an endpoint if
you already knew its ID — but you could not ask *"which endpoint sends a message to a
channel?"*

### Why this is genuinely hard

Search "send a message" and there are **hundreds** of plausible answers across 118
providers: Slack, Twilio, SendGrid, Discord, Zoom, plus every email API. In SendGrid
alone there are 14 endpoints that sound right.

That ambiguity is the actual engineering problem. With only 2 providers, "send a
message" would have one answer and the search engine would be pointless.

### Method 1 — Keyword search (BM25)

**The idea:** find endpoints whose text contains your words, ranked so that rarer
words count for more. Searching "slack message" — "message" appears everywhere so it
counts little; "slack" is rare so it counts a lot.

BM25 is the refined version of this idea, decades old, still the backbone of most
search engines. It is not AI, it is arithmetic, and it is very fast.

**The clever bit we had to add.** An endpoint's address is `/chat.postMessage`. To a
computer that is one meaningless blob, so searching "post message" would never find
it. We split addresses into words:

```
/repos/{owner}/{repo}/issues   →   repos owner repo issues
/chat.postMessage              →   chat post message
```

This turned out to be **essential**: Slack's spec has no descriptions at all, so the
address is the only text there is. Without splitting, Slack would be invisible.

**The result:**

```
Search: "send a message to a channel"

  1.  POST /chat.postMessage         slack_com     ← rank 1 of 10,563
  2.  POST /chat.postEphemeral       slack_com
  3.  POST /chat/users/{userId}/messages   zoom_us
  4.  POST /api/v2/Notifications     agco_ats_com
```

**Correct answer, first place, in about 13 milliseconds.** The runners-up are
genuinely reasonable — that is what a real ranking problem looks like.

### Method 2 — Meaning search (embeddings)

Keyword search has one fatal weakness: **if you use different words, it finds
nothing.** Search "notify my team" and BM25 fails completely, because Slack's
documentation says "post a message" — not a single word in common, identical meaning.

**Embeddings** fix this. An AI model converts text into a list of numbers
representing its meaning. Similar meanings produce similar numbers, even with no
shared words. We convert all 10,563 endpoints into numbers once, then convert your
search into numbers and find the closest matches.

### Method 3 — Hybrid (using both)

Neither wins alone:
- **Keyword** is unbeatable on exact terms — `chat.postMessage`, `POST`, `repos`
- **Meaning** wins on paraphrase — "notify my team" → "post a message"

We combine them using **Reciprocal Rank Fusion**. Rather than trying to compare two
incompatible scoring scales, it only looks at *positions*: something ranked #2 by both
methods beats something ranked #1 by one and ignored by the other. Simple, robust,
one setting.

### All three working — and what the comparison showed

All 10,563 endpoints are now converted to meaning-numbers, so we could finally run the
same question through all three methods. The results were more interesting than
"the new one is better".

**Where meaning-search clearly won.** Ask *"charge a customer credit card"*:

| Method | Top result |
|---|---|
| Keyword | BeezUP's "get credit card information" ❌ — matched the literal words |
| Meaning | **Stripe's `/v1/charges`** ✅ — understood the intent |
| Both | **Stripe's `/v1/charges`** ✅ |

Keyword search fixated on the exact phrase "credit card" and put Stripe's actual
payment endpoint third. Meaning-search understood that *charging a card* is what
`/v1/charges` does, even though the endpoint never says "credit card" anywhere.
**This is precisely the weakness that embeddings exist to fix**, caught in the wild.

**Where combining them won.** Ask *"send a message to a channel"*:
keyword put Slack's `chat.postMessage` first; meaning-search put a different
messaging company (Ably) first and Slack second. Combining them restored Slack to
first place — each method covered the other's blind spot.

**Where combining them lost.** Ask *"notify my team when something breaks"* and the
combined result was *worse* than either method alone. It promoted a result that
keyword ranked 18th and meaning ranked 49th — both bad, but bad *consistently*, and
the fusion rewards agreement. A real weakness, worth knowing and worth measuring
rather than hiding.

**The cost: speed.** Meaning-search must convert your question into numbers before it
can compare anything, and that takes about a third of a second on this machine:

| Method | Typical speed |
|---|---|
| Keyword | **11–50 ms** |
| Meaning | ~350–420 ms |
| Both | ~380–480 ms |

Our target is under 500 ms, so hybrid *just* fits — with little room to spare. That is
a genuine trade-off to measure on Day 3, not a detail to gloss over: three times
better answers for thirty times the wait may or may not be worth it.

### What we built

| File | What it does |
|---|---|
| `search/text.py` | Splits addresses into words, removes filler words, expands synonyms |
| `search/index.py` | Builds the fast keyword lookup structure |
| `search/embeddings.py` | Converts endpoints into meaning-numbers and stores them |
| `search/retrieve.py` | The three search modes and the combining logic |
| `api/main.py` | A web service so a browser can use all of this |

### An honest negative result

Searching *"create a new issue in a repository"* put GitHub's endpoint at **rank 7**,
behind Bitbucket's.

The cause is a beautiful little detail. Search engines reduce words to stems so
"running" matches "run". But the stemmer turns "repository" into `repositori` and
"repos" into `repo` — **different stems**. So the query never matched GitHub's
address, while Bitbucket's `/repositories/` matched exactly.

We added a rule teaching the system that repo/repos/repository are the same word.
GitHub's *score* went up — and its *rank went down to 16*, because the rule helped
Bitbucket even more.

**So we stopped tuning.** Adjusting settings based on one query is how you fool
yourself: you make one example look good and quietly make everything else worse. The
synonym feature is now a switch that Day 3 will test properly against 30 queries with
known correct answers, and the data will decide. *That is a better outcome than a
tuned demo, even though it looks worse right now.*

### The API-key adventure (and why the design changed)

This one is worth reading, because it changed a design decision.

**You put your key in `.env.example`.** That file is the *template* meant to be shared;
`.env` is the private one that never leaves your machine. Moved it. Nothing had been
committed anywhere, so no harm done.

**Then I leaked it into a log file.** I was sending the key as part of the web address
(`...?key=YOUR_KEY`). When the service replied "too many requests", the error message
included the full address — key and all — and that went into a log.

This broke *our own rule* that secrets must never appear in logs. Fixed properly: the
key now travels in a hidden header instead of the address, plus a safety net that
strips it from any error text. The old log was scrubbed. **Rotating the key is still
the careful thing to do.**

**Then the model names turned out to be retired.** The error was a "404 not found" —
not "wrong key". Authentication had *succeeded*; the models simply no longer exist.
Updated to current ones.

**Then the real blocker.** Google's free tier allows **1,000 embeddings per day**. We
need 10,563. That is 11 days of waiting, and no amount of clever batching gets around
a daily cap.

**So we changed approach: embeddings now run on your own computer.** A small AI model
(130 MB) downloads once and runs locally forever.

This is better than the paid API even ignoring the quota:
- Free, unlimited, no key required for search
- **Anyone who downloads your project can run it.** A portfolio project that needs
  someone else's paid API key to demonstrate is a worse portfolio project.
- The same input always gives the same numbers, so your benchmark results are stable

Your Gemini key is still needed — Day 5's planning step uses a different service with
a much more generous free allowance.

**And one bug that was my own fault.** The first local run stalled halfway. The model
was fine; my code was storing millions of individual numbers as separate Python
objects and re-compressing the entire growing file at every save. Rewritten to use
efficient numeric arrays.

Then I *measured* instead of assuming: ~4 endpoints per second on your machine, so
about 45 minutes total. Your computer has 22 processor cores, and forcing the model to
use all of them made it *slower* (they spent more time coordinating than working). So
45 minutes is simply the honest cost — free, one-time, and it saves its progress as it
goes so an interruption never loses work.

---

## Day 3 — Measuring how good the search actually is

**Goal: stop *saying* the search is good and start being able to *prove* it.**

Until today every claim about search quality was an opinion. On Day 2 we looked at
four example questions and formed impressions. That is exactly how people fool
themselves: you pick examples that work, and never notice the ones that don't.

### How you measure a search engine

You write down a list of questions **together with the answers you already know are
correct**, then let the search engine answer them and compare. That list is called a
*labelled query set*, and building it honestly is most of the work.

We wrote **30 questions**, each listing every genuinely correct endpoint.

Two rules made it honest:

**1. The answers were found using plain database lookups, never using our own search
engine.** If you label whatever your search returns, you are grading its homework
against its own answer sheet — it scores brilliantly and the number means nothing.

**2. Multiple providers can be correct.** "Create an issue" is genuinely satisfied by
GitHub, Bitbucket *and* Jira. Marking only GitHub would punish the engine for being
right in a way we failed to anticipate. This also resolved a puzzle from Day 2, where
Bitbucket outranked GitHub and looked like a failure — Bitbucket was a correct answer
all along.

### The four scores

| Score | Plain meaning |
|---|---|
| **P@5** | Of the top 5 answers, how many were right? |
| **R@20** | Of all the right answers that exist, how many made the top 20? |
| **MRR** | How near the top was the *first* right answer, on average? 1.0 = always first. |
| **NDCG@10** | Overall ranking quality, giving more credit for right answers near the top. |

**R@20 matters most for us.** Later, the AI planner only ever sees the top 20
candidates — anything below that is invisible to the entire rest of the system.

### The results

| Method | P@5 | R@20 | MRR | NDCG@10 | Speed | Failed completely |
|---|---|---|---|---|---|---|
| Keywords | 0.147 | 0.517 | 0.366 | 0.301 | **11 ms** | 8 of 30 |
| Meaning | 0.187 | 0.601 | 0.430 | 0.358 | 388 ms | 6 of 30 |
| **Both** | **0.207** | **0.601** | **0.505** | **0.412** | 453 ms | 6 of 30 |

**Combining both wins on every quality measure.** Against keywords alone it improves
ranking quality by 37% and roughly halves how far you scroll to the first right
answer.

Do not be alarmed that P@5 looks low. Many questions have only one correct answer, and
P@5 divides by 5 regardless — so 0.2 is the highest score possible for those. What
matters is the comparison between the three methods, not the distance from 1.0.

### Two experiments, and one of them failed

**Experiment 1: were the synonyms worth keeping?** This was the question left open on
Day 2, when one example suggested they made things worse. Measured across all 30
questions, they clearly help — ranking quality up 21% for keyword search, and two
fewer complete failures.

**This vindicates the Day 2 decision to stop tuning and build the benchmark.** The
single example had pointed the wrong way. Had we "fixed" it then, we would have made
the system genuinely worse while believing we had improved it.

**Experiment 2: should synonyms work in both directions?** Our list says "issue also
means bug" but not "bug also means issue". Since synonyms are symmetric by definition,
making the reverse automatic looks like an obvious correctness fix.

Measured, it was **worse**: ranking quality fell from 0.301 to 0.241, and complete
failures rose from 8 to 11. It fixed one question and broke five.

The reason is worth understanding: **adding search terms is not free.** Every extra
word dilutes the signal, and making everything symmetric created chains like
*user to member to account*, dragging in loosely related endpoints. The change was
reverted, with the measurement recorded in the code so nobody tries it again.

*A theoretically correct idea that the data rejected.* That is what a benchmark is
for, and it is a far better thing to say in an interview than "I added synonyms and
it felt better."

### What still doesn't work

Three questions get **no correct answer from any method**:

| Question | Why |
|---|---|
| "open a bug report in a code repository" | Every provider says "issue"; the question says "bug report" |
| "schedule a video conference call" | Zoom says "meeting"; nobody says "video conference" |
| "propose code changes for review" | Everybody says "pull request" |

We wrote these as deliberate traps, and **meaning-search failed them too** — a useful
correction to the common belief that embeddings simply solve the vocabulary problem.

We are deliberately *not* fixing them by adding matching synonyms. That would be
tuning the system to the exact test we grade it with, which inflates the score and
teaches us nothing. The honest fixes are a larger question set, a second ranking
stage, or a better embedding model — each measured against fresh questions.

### The speed problem

The project targets answers in under half a second.

- Keyword search: **11 ms** — 45x faster than needed
- Both combined: 453 ms typical, and **510 ms for the slowest 5%** — just over budget

Almost all of that is the third of a second spent converting your question into
numbers, not the searching itself. Real options: remember recent questions, use a
smaller model, or use fast keyword search while someone is typing and reserve the
slower, better hybrid for when the AI is planning — where 400 ms is nothing next to
the several seconds an AI call takes anyway.

**This is a genuine trade-off, recorded rather than hidden.**

---

## Day 4 — The validator

**This is the day the project earns its central claim.** Everything before it was
groundwork: a database of API manuals, and a way to search them. Today we built the
part that makes the AI safe to use.

### What the validator is

It takes a proposed automation and checks **every claim in it** against the database
before anything is allowed to run. If any check fails, the automation is refused and
you are told exactly which step, which field, and why.

The AI is never consulted during this. It is ordinary code asking mechanical
questions with definite answers.

### The seven checks

Run in order, cheapest first, so a failure reports the cause rather than the
symptoms:

| # | Check | Example of what it catches |
|---|---|---|
| 1 | **Structure** | Steps that loop forever, or are never reached |
| 2 | **Existence** | An endpoint the AI invented that does not exist |
| 3 | **Agreement** | The plan claims `GET`; the real endpoint is `POST` |
| 4 | **Parameters** | The repository name was never supplied |
| 5 | **Body** | Slack requires `channel`; nothing provides it |
| 6 | **Compatibility** | Putting a whole person object where text is required |
| 7 | **Policy** | Unsupported login; a delete with no human approval |

### It works on the real data

The genuine GitHub-to-Slack workflow now compiles against the real corpus:

```
COMPILED  order = [n1, n2]
  n1: POST https://api.github.com/repos/{owner}/{repo}/issues
  n2: POST https://slack.com/api/chat.postMessage
      body.channel <- "#eng"
      body.text    <- n1.title
```

**Notice what the plan never contained: a web address.** The plan only names an
endpoint *identifier*. The address, the method and the login method are all filled in
from the database afterwards. This is not a detail — it is the mechanism. There is no
code path where an address written by an AI can reach the internet, so a fabricated
or malicious destination is impossible rather than merely unlikely.

### The four rejections, on real data

**1. An invented endpoint.** We replaced Slack's identifier with `ep_totallyfake99`:

> `endpoint 'ep_totallyfake99' does not exist in the knowledge base`
> *hint: endpoint ids must come from a search result, never be constructed*

This is the whole project in one message. And recall the identifiers are deliberately
unguessable strings like `ep_18da3450e690` — the AI cannot even produce a plausible
fake.

**2. The wrong field name.** We asked for `author`, the mistake everyone makes:

> `/repos/{owner}/{repo}/issues does not return 'author'`

Caught before running, not after. GitHub returns `user.login`.

**3. A type mismatch.** We put the whole `user` object where Slack wants text:

> `'user' is a object but 'text' needs a string`

**4. A dangerous action with no approval.** Deleting a repository with no human
confirmation step is refused outright.

### Why we built this *before* the AI

The AI planner is tomorrow. Building the gate first means the thing being checked
arrives after the checker — and if tomorrow goes badly, you still have a working
system where automations are written by hand and rigorously verified. **The AI is the
optional layer.** That is the architecture stated as a schedule.

### A mistake we made and corrected

The first run refused our own flagship demo. Slack's manual declares its login method
as "OAuth 2.0", and we had marked OAuth as unsupported.

On inspection our rule was simply wrong. What we cannot do is the OAuth *dance* —
bouncing through a browser, exchanging codes, refreshing tokens. But **using a token
you already have is trivial**: it goes in a header, exactly like any other key. That
is precisely how a Slack bot token and a GitHub personal access token work.

Being too strict here would have rejected most of the 118 providers for a limitation
that does not exist at the moment of the call. Reclassified, with genuinely impossible
methods (mutual TLS, request signing) still refused.

*Worth noticing: the validator was doing its job correctly. The data it was given was
wrong. That distinction is most of debugging.*

### Where the validator deliberately stops

It **cannot** prove your automation does what you meant. It can prove `user.login`
and `user.name` are both text; it cannot know which one you wanted. Human review
still matters, which is why you can edit a plan before turning it on.

It also does not understand every possible schema. Some manuals use constructs like
"this field is either a string or a number", which our checker cannot reason about.
For those it says *unknown* and allows them through.

**That direction is chosen deliberately.** This is a gate against provably wrong
plans, not a proof of correctness. Refusing everything we cannot model would reject
most real APIs and make the system useless. The limits are written down in the code
rather than hidden.

### What was built

| File | What it does |
|---|---|
| `workflow/dag.py` | The shape of an automation, and of a rejection reason |
| `workflow/schema_match.py` | Does data from step A fit step B? |
| `workflow/compiler.py` | The seven checks and the compiled output |
| `workflow/store.py` | Saving automations, versioned by insert |

Plus 74 new tests (**123 total**), including six deliberately broken plans that must
each be refused *for the right reason* — a validator that rejected everything would
pass a weaker test, so each test names the exact expected failure.

---

## Day 5 — The AI planner

**Goal: type an English sentence, get a validated automation out the other end.**

Everything until now was the machinery. Today the pieces were joined up, and the
system does what the project promised on page one.

### It works

```
$ uv run engine plan "When a new GitHub issue is created,
                      send a Slack message with the issue title and author"

intent    : trigger='a new issue is created in a repository'
            actions=['post a message to a channel']
candidates: 16 endpoints retrieved
attempts  : 1   tokens: 2582
  attempt 1: accepted

COMPILED  order: n1 -> n2
  n1 [trigger]  POST https://api.github.com/repos/{owner}/{repo}/issues
  n2 [api_call] POST https://slack.com/api/chat.postMessage
      body.channel <- '#general'
      body.text    <- n1.body
```

A sentence became a checked, runnable automation in about twenty seconds, for
roughly 2,500 tokens — a fraction of a cent.

**And it is not a GitHub/Slack special case.** Asked for *"when a Stripe payment
fails, create a Jira issue about it"* it produced a valid compiled workflow between
an entirely different pair of providers.

### The three steps

**1. Understand.** The sentence becomes a structured checklist — the trigger, the
actions, and the data that must flow. Crucially, it is rewritten into the language
API documentation uses: "Slack me" becomes "post a message to a channel". That is
what the search engine can actually match.

**2. Search.** One search per part of the checklist, over all 10,563 endpoints,
using the hybrid method Day 3 measured as best. Sixteen candidates came back.

**3. Propose and check.** The AI receives those sixteen candidates — with their
mandatory fields and the fields they return — and proposes a plan. That plan then
faces Day 4's seven checks.

### The two guards, and why their order matters

**Guard one: the candidate list.** The AI may only name endpoints that *this
request's searches actually returned*. Anything else is refused **before the database
is even consulted**.

This is stronger than it first sounds. We tested it with a real endpoint that exists
in the database but was not among the sixteen offered — it was still refused.
Existing is not enough; it must have been *retrieved for this goal*. That closes the
gap where a model recalls a genuine endpoint from training data and uses it out of
context.

And because identifiers are unguessable strings like `ep_18da3450e690`, the AI cannot
even produce a convincing fake. **The format of the identifier is itself a defence.**

**Guard two: the compiler.** Anything surviving guard one still faces all seven
deterministic checks from Day 4.

### The repair loop

When the compiler refuses a plan, it does not simply fail. The exact reasons go back
to the AI:

```
YOUR PREVIOUS PLAN WAS REJECTED:
  [unresolvable_source_path] node=n2 field=body.text:
      /repos/{owner}/{repo}/issues does not return 'author'
      HINT: check the field path against the endpoint's response schema
```

The AI tries again with that information, up to three times, then gives up rather
than looping forever.

**This is why validation errors were built as structured objects rather than
sentences back on Day 4.** The same data drives the screen a person reads and the
correction the AI receives. One decision, two payoffs.

We also hand the AI each candidate's mandatory fields and available response fields
*up front*. It cannot supply a required parameter it was never told about — and
indeed both real runs were accepted on the first attempt, needing no repair at all.

### Two real problems we hit

**The model was retired mid-project.** `gemini-2.5-flash` returned "no longer
available to new users; please use gemini-3.6-flash". A one-line configuration
change — which is precisely why the model name lives in configuration and not in the
code.

**The AI's thinking ate its own answer.** The first real run returned fragments of
reasoning instead of a plan: *"Wait, title is NOT in the returns..."*

The cause is worth understanding. Modern models "think" before answering, and those
thinking tokens are charged against the same output budget as the answer. On a long
planning prompt the reasoning consumed the entire allowance and the reply was cut off
mid-thought.

Three fixes: cap how much the model may deliberate, ignore any reasoning it returns
separately, and — when there is no answer at all — say *"the model ran out of output
budget"* rather than *"could not parse JSON"*. The error you see should name the
cause, not the symptom.

### An honest limitation, visible in our own demo

Look again at the successful run. We asked for the issue **title and author**. The AI
mapped `body.text <- n1.body` — the issue's *description*.

That is a valid workflow. Every field exists, every type matches, it would run
without error. **It is simply not quite what we asked for.**

This is exactly the boundary described on Day 4: the validator proves an automation
*can* run, never that it does what you *meant*. `title`, `body` and `user.login` are
all text, and no amount of checking can tell which one was in your head.

That is why you review a plan before switching it on, and why the visual editor
matters. Anyone claiming their AI system removes the human entirely is either not
validating, or not being straight with you.

### What was built

| File | What it does |
|---|---|
| `agent/llm.py` | Talking to Gemini or OpenAI, with a stub for tests |
| `agent/intent.py` | Sentence to structured checklist |
| `agent/planner.py` | Search, propose, guard, repair |

Plus 25 tests (**148 total**), all using a scripted fake AI. Real models are slow,
cost money, and give different answers each time — fine for a benchmark, useless in a
test suite that must fail only when the code is wrong.

---

## Day 6 — Actually running it

**Goal: stop describing automations and start performing them, reliably.**

Until today the system could plan and verify an automation but never carried one
out. Today it does — against real servers, with the safeguards that separate a demo
from something you would leave running.

### It really runs

We pointed it at a public test server that echoes back whatever it receives, so we
can prove exactly what was sent:

```
1. REAL EXECUTION
  status: succeeded
    n1: succeeded  http=200  1960ms  attempts=1
    n2: succeeded  http=200  2014ms  attempts=1
    n1 sent : {'issue': {'title': 'Login is broken'}}
    n2 sent : {'text': 'Login is broken'}   <- taken from n1's real reply
```

Two genuine HTTP calls, and the second one carried a value read out of the first
one's actual response. That is the whole promise of the project, performed rather
than described.

### The crash test — the one that matters

Here is the scenario that ruins naive automations. Your program sends the Slack
message, then dies a fraction of a second later, before it managed to record that it
had sent it. The job is handed out again. **Does the team get a second message?**

```
2. CRASH TEST: re-run the same execution
  node results before: 2   after: 2
  checkpoints untouched: True
  -> every node was skipped; no HTTP call was repeated
```

**Zero duplicates.** Two things make that true.

**Checkpoints, written in the right order.** After each step finishes, its result is
saved *before* the step is marked done:

```
   run the step  ->  save its result  ->  only then mark it done
```

Doing it the other way round would be a disaster: a crash between marking and saving
loses the work silently, and silent loss is worse than doing something twice, because
nobody notices.

**Idempotency keys.** Every state-changing call carries a fingerprint derived from
the execution, the step and the exact payload. Repeat the call and the fingerprint is
identical, so the provider can recognise it as the same request rather than a new
one. A replay is therefore *safe*, not merely *unlikely*.

### The 200 that isn't a success

Remember the trap from Part 1: Slack often replies `200 OK` — the code that normally
means success — while hiding the real failure inside the reply:

```json
{"ok": false, "error": "channel_not_found"}
```

The executor reads the body, not just the status code. A reply like that is recorded
as a failure, with the provider's own reason attached. Without this, the automation
would cheerfully report success while posting nothing, which is the single most
misleading failure a system like this can have.

### Retries that know when to stop

Not every failure deserves another attempt:

| What happened | Retry? | Why |
|---|---|---|
| Service briefly unavailable (500) | yes | It may well work in a moment |
| Too many requests (429) | yes, after waiting the time they ask for | They told us when to come back |
| Network timeout | yes | Networks hiccup |
| **Bad credentials (401)** | **no** | It will never succeed; retrying just wastes quota |
| **Wrong field in the request** | **no** | The request itself is wrong |

Waiting time doubles between attempts, with a random wobble added. The wobble matters
when many workers were all affected by the same outage — without it they would all
come back at the identical instant and knock the service over again.

### Secrets

Tokens are **encrypted** in the database. They are decrypted for the instant of a
call and dropped immediately afterwards. They never appear in a log line, an error
message, the execution history, or anything shown to the AI.

Providers do sometimes echo your token back inside an error message, so error text is
scrubbed before it is stored — there is a test that puts a token in a fake error
reply and asserts it never survives.

Every use is recorded in an append-only audit log: which credential, which execution,
which step, when.

### Refusing to call the wrong place

Even though addresses can only come from the database, there is a second check
immediately before dispatch. This one refuses:

- Anything on your own machine or private network
- **The cloud metadata address** — a special address reachable from inside a server
  that will hand out its access keys. It is the classic target of this kind of
  attack.
- Any address scheme that is not ordinary web traffic
- Any provider not on the explicitly permitted list
- Redirects, unless the new destination passes the same checks

Demonstrated live: pointing a workflow at the metadata address produced
`policy: host '169.254.169.254' is a private address`, and no request left the
process.

### A gap the demo found

The dry run — build the real request, deliberately do not send it — worked for the
first step and **failed for the second**.

The reason is obvious in hindsight. In a dry run nothing actually ran, so step one
produced no reply, so the value step two wanted did not exist, so it errored.

The fix: during a dry run, a value that cannot exist yet is filled with a visible
placeholder like `<dry-run:n1.title>`. You still see the real address, the real
method, and the real request shape — which is the entire point.

A real run still fails properly on a missing value; there is a test asserting exactly
that, because a convenience for one mode must never weaken the other.

### What was built

| File | What it does |
|---|---|
| `execution/credentials.py` | Encrypted storage, use-time decryption, audit trail |
| `execution/guards.py` | Refusing internal, private and impermissible destinations |
| `execution/executor.py` | Building and sending one request, retries, failure classification |
| `execution/runner.py` | Order, checkpoints, resumption, the job queue |
| `execution/worker.py` | The background process, with graceful shutdown |

Plus 58 tests (**206 total**), every outbound call faked so the suite never sends a
real message.

### Two bugs of our own

**We checked the destination too late.** The original order resolved the credential
first and checked the address second — so a request destined to be refused still
decrypted a secret and wrote an audit entry. The address is now checked first: never
touch a secret for a call you are not going to make.

**Two different clocks.** One part of the queue compared times in UTC while a test
used local time. Five and a half hours apart, so an expired lease looked like a
future one and a crashed worker's job would never have been picked up. Now there is
one helper and one convention. *Mixing time conventions is a classic source of bugs
that only appear in some timezones — which is the worst kind.*

---

## Day 7 — The interface, and a bug hunt

Two jobs on the last day: give the system a face, and go looking for the bugs that
accumulate when you build quickly.

### The bug hunt came first

Reading back over the code turned up six real defects. Five were found by reading;
one was found by using the thing.

**1. Failed requests were retried when they never could succeed.** The most serious
one. Every four-hundred-series error — *bad request*, *not found*, *unprocessable* —
was being filed under "provider problem", and provider problems get retried three
times.

But a 400 means *the request itself is wrong*. Sending it again changes nothing. It
burns quota and delays the real error reaching you. Now four-hundreds are treated as
a validation failure and fail immediately, while five-hundreds — genuine server
trouble — still get another attempt. There is a test covering nine status codes so
this cannot quietly regress.

**2. Two different clocks, again.** Twelve places recorded times *with* a timezone
while the queue compared times *without* one. The same category of bug found on Day
6, still lurking elsewhere. All standardised.

**3. A private helper reached across three modules.** A function named with a leading
underscore — Python's convention for "internal, do not touch" — was being imported
by three other files. Either it is internal or it is not. Made public and documented.

**4. A setting that did nothing.** How long a worker may hold a job before others may
reclaim it was configurable, and the code ignored the setting and used a hardcoded
five minutes. Now wired up.

**5. Three pieces of dead code** — functions and constants written, never called.
Removed, with a comment explaining *why* one of them was unnecessary (redirects are
refused outright rather than followed and re-checked).

**6. A dry run insisted on credentials.** This one only appeared when using the web
page: previewing an automation demanded that every provider's token already be
configured. Which is backwards — the whole point of a preview is to inspect a plan
*before* wiring it up. A dry run now reports a missing credential instead of failing
on it, while a real run still refuses to proceed without one.

*Worth noticing that the ones found by reading were mostly tidiness, and the one
found by using it was the one that actually blocked a person.*

### The web interface

Three pages, deliberately plain.

**Search.** Type a capability, pick keyword / meaning / both, see ranked endpoints
with their scores and — for hybrid — where each ranked in the two underlying methods.
Live counts of what is indexed sit at the top.

**Create.** A sentence goes in. You get back the model's understanding, the plan it
proposed, and **a checklist of all seven validation checks** with green ticks and red
crosses. When something fails you see the precise reason and its hint, not a generic
error. Then two buttons: *Dry run* builds the real request and sends nothing; *Run
for real* does.

**Runs.** Every execution, expandable into a per-step timeline with attempt counts,
status codes and timings.

The plan view deliberately shows the **full addresses** — `POST
https://slack.com/api/chat.postMessage`. That is the point being made visually: those
came from the database, not from the AI.

### The one architectural rule in the frontend

The browser never talks to the engine directly. Every request goes through the
website's own server first, which forwards it on.

This is not indirection for its own sake. It means the engine's address — and later,
any session token — stays on the server and is never shipped to a browser. The rule
is that this layer only attaches credentials and forwards; **no logic lives there.**

### Verified working together

```
1. PLAN (through the website's server)
   http 200  ok=True  attempts=1  tokens=2707
   n1: POST https://api.github.com/repos/{owner}/{repo}/issues
   n2: POST https://slack.com/api/chat.postMessage

2. DRY RUN
   status=succeeded   n1: succeeded 56ms   n2: succeeded 60ms

3. HISTORY
   8 executions recorded
```

### One more security fix

Installing the web framework produced a warning: the version we picked had a known
security hole. Upgraded to a patched release; the audit now reports zero
vulnerabilities.

*A warning during installation is not noise. It is the cheapest security fix you will
ever get, and it takes one command.*

### Two more bugs, found by looking at the screen

Using the finished page turned up two more problems that no test had caught.

**A preview refused to preview.** Asking for a dry run of a workflow using Box
produced a red failure: *"provider 'box_com' is not on the execution allowlist"*.

The allowlist is the short list of providers permitted to be *called for real* — a
safety catch so an automation cannot contact a service you have not deliberately
enabled. Perfectly sensible for a real run. But this was a **dry run**, which sends
nothing. The request had been built correctly; it was rejected for a rule about
sending.

Worse, it made the feature useless exactly when you need it: previewing a workflow
for a provider you have *not* yet enabled is the whole point.

This is the same mistake as the credential one fixed on Day 7, sitting two lines
above it in the same function, and it was missed. Now a dry run **reports** what
would stop it rather than failing:

> ⚠ would not run · step n2 — provider 'box_com' is not on the execution allowlist

You get the whole truth: the request is well formed, *and* it would not be sent. A
real run still refuses outright.

**The method ran into the address.** The page showed
`POSThttps://api.box.com/2.0/...` with no space.

The cause is a small lesson in how styling rules work. The layout rule was written as
"a header *inside a result box*", and on the plan page that header sits inside a
*step box* instead. The rule simply did not apply, so the pieces collapsed together.
Written correctly it now applies everywhere the header appears.

### One thing that looked like a bug and was not

The same screenshot showed a plan sending an SMS via `GET /messages/send`, which
looks obviously wrong — you do not usually *fetch* something to send a message.

Checking the database: BulkSMS really does publish that endpoint, and its own
description reads *"Send message by simple GET or POST"*. The plan was correct.

The genuinely odd part — using "add an email alias" as a starting event — is the
limitation described on Day 4 and Day 5, not a defect. Every endpoint exists, every
type matches, so nothing can prove it is not what you meant. **That is the boundary,
and it is why you look at a plan before switching it on.**

---

## After the build — a real prompt that failed, and the three faults behind it

The first genuine use of the finished system was the prompt *"whenever I get a Gmail,
send me a WhatsApp message"*. It produced a workflow that added an email alias in Box
and sent an SMS through BulkSMS.

Structurally perfect. Completely wrong. Three separate faults, none of them the two
bugs fixed on Day 7, and **all three were mine**.

### Fault one: Gmail was never downloaded

Checking the database: no Gmail, no WhatsApp. The only Google service present was a
machine-learning API.

The cause is a rule I wrote on Day 1 and was pleased with. The downloader keeps **one
specification per provider**, because GitHub publishes about twenty near-identical
copies (the public API plus every Enterprise edition) and without the rule a corpus
of twelve specs was twelve GitHubs.

But `googleapis.com` is not twenty copies of one API. It is **281 entirely different
products** — Gmail, Calendar, Drive, Sheets. My rule kept exactly one of them and
discarded the other 280. Gmail was among the discarded.

Both cases look identical from the outside: *provider name, colon, a suffix*. What
separates them is the API's own title. Every GitHub edition publishes the title
"GitHub v3 REST API"; Gmail and Calendar publish different titles. So the rule now
groups by **provider *and* title**, which collapses genuine duplicates and keeps
genuine products.

The effect was not marginal:

| | Before | After |
|---|---|---|
| Distinct APIs the downloader could see | 676 | **2,080** |
| APIs in the database | 118 | **270** |
| Endpoints | 10,563 | **15,504** |

Roughly two thirds of the available catalogue had been invisible the whole week.

### Fault two: I told the AI to guess

Buried in my instructions to the planner was this line:

> *"If no candidate fits, use the closest one rather than inventing an identifier."*

I wrote it while concentrating on a different danger — stopping the AI from making up
endpoint identifiers. In closing that door I opened a worse one: I explicitly
instructed it that when nothing fits, **substitute the nearest thing**.

So it did. Asked for Gmail it found no Gmail, took the closest email-shaped endpoint,
and built something that passed all seven checks.

The AI can now answer honestly:

```json
{"cannot_satisfy": {"reason": "...", "missing": ["gmail", "whatsapp"]}}
```

and the instructions now say why this matters: *a plan built from the nearest
available endpoints is worse than no plan — it looks correct, passes every
structural check, and does something nobody asked for.*

Re-running the prompt, the system now says:

> **REJECTED** — Neither Gmail trigger endpoints nor WhatsApp message action
> endpoints are available in the candidate list.

An honest "I cannot do this" is a *feature*. The failure mode it replaces —
confident nonsense — is the one that actually costs people money.

### Fault three: the search was throwing away the best clue

Even with Gmail in the database, the search still did not find it. The reason was
visible in the system's own output:

```
you asked : "whenever I get a Gmail, send me a WhatsApp message"
trigger   : "receive a new email"        <- Gmail deleted
actions   : ["send a message"]           <- WhatsApp deleted
```

The step that turns your sentence into a structured request was **removing the
service names**, then searching for a generic operation across every provider at
once.

My instructions were self-contradictory. The rule said *"do not name specific
companies unless the request does"* — correct. But the worked example beneath it
read:

> *"Slack me" becomes "post a message to a channel"*

which deletes "Slack". **The example taught the opposite of the rule, and the example
won.** Models follow demonstrations more reliably than they follow prose.

Rewritten so the examples demonstrate the rule, and the difference is measurable:

| Search for | Gmail or WhatsApp found? |
|---|---|
| "receive a new email" | ✗ nowhere in the top results |
| "receive a new email **in Gmail**" | ✓ rank 2 |
| "send a message" | ✗ nowhere |
| "send a **WhatsApp** message" | ✓ rank 3 |

The name of the service is the single most distinguishing word in the whole request.
Throwing it away left the search hunting for "email" among a hundred providers.

### What this episode is actually about

Three faults, three different layers — what gets downloaded, what the AI is told,
what the search is given. **Not one of them was a crash.** Every piece reported
success. The database said it had ingested; the search said it had found; the AI said
it had planned; the validator said every check passed.

The system was confidently, silently wrong, and only became visible because someone
typed a real request and looked at the answer.

*The tests could not have caught any of this.* Every test used fixtures I wrote, and
my fixtures contained the endpoints my code expected. That is the honest limit of a
test suite: it proves the code does what you think, never that what you think is
right.

### Fault four: every automation was forced to have an event

With the first three fixed, the rephrased goal *"read my latest Gmail and send it as
a WhatsApp message"* still failed — and the reason was visible in the output:

```
trigger : receive a new email in Gmail     <- invented; the request has no event
```

My requirement format **always demanded a trigger**, so the AI manufactured one for a
request that was plainly a sequence of actions. The search then went hunting for an
event endpoint nobody had asked for.

Plenty of useful automations have no event at all: *do this, then that, when I ask.*
A trigger can now be the word `"manual"`, and the workflow's first step is simply its
entry point.

### Fault five: the AI could not name a field it was never shown

Next attempt, it produced a plan and then refused with an unusually precise
explanation:

> *"The list messages endpoint does not define item-level payload structures or
> indexable nested paths in its returns schema (only 'messages'), making it
> impossible to deterministically extract the specific message ID."*

**It was right, and it had found a bug in my code.** Gmail's list endpoint returns
`{messages: [...]}` — a list. To read one you need `messages[].id`. But the summary
of available fields I hand to the AI listed only top-level names and one level of
nested *objects*; it never looked **inside lists**. So `messages[].id` was invisible,
and the AI concluded — correctly, from what it could see — that the job was
impossible.

Fixed, and the workflow then compiled.

### Fault six: the checker and the runner disagreed

The compiled plan contained `n1.messages[0].id`. The validator accepted it. And the
part that actually reads values at runtime **could not follow it** — it understood
`messages[]` but not `messages[0]`.

That is the worst kind of bug this project can have: a workflow that passes every
check and then fails while running, which is precisely what the validator exists to
prevent. Two separate pieces of code interpreted the same notation differently, and
nothing forced them to agree.

Now they share the same pattern, and a test asserts they always will — including one
that takes each path the checker approves and confirms the runner can read it.

### It works

```
GOAL: "read the latest message from my Gmail inbox and send its
       contents as a WhatsApp message"

PLAN: COMPILED (2 attempts, 5850 tokens)
  n1  GET  https://gmail.googleapis.com/gmail/v1/users/me/messages
  n2  GET  https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}
        id <- n1.messages[0].id
  n3  POST http://whatsapp.local/messages
        text.body <- n2.snippet

DRY RUN: succeeded — all three requests built
  would not run: providers not yet enabled, no credentials configured
```

Three steps across two real services, values chained from one response into the next,
built and checked without sending anything. The first attempt was rejected by the
validator and the second corrected it — the repair loop earning its keep on a real
request rather than a test.

The remaining warnings are honest and expected: nobody has enabled those providers or
supplied tokens.

**What still does not work is the original wording.** *"Whenever I get a Gmail"* asks
for something to happen by itself, and this system has no way to watch for events —
no scheduled checking, no webhooks. That is on the "not built" list in the README, and
no amount of fixing search will change it. The system now says so plainly instead of
inventing a plan.

---

# Part 5 — Your project, file by file

```
tutorial project/
│
├── EXPLAINED.md          ← this document
├── README.md             the public front page
├── architecture.md       the technical design
├── techstack.md          every technology choice and why
├── conventions.md        the coding rules
├── env.md                setup and settings reference
├── todo.md               the long-term nine-phase roadmap
│
├── .env                  YOUR SECRETS — never share, never commit
├── .env.example          the blank template — safe to share
├── pyproject.toml        the list of software libraries we depend on
│
├── data/                 (not shared — rebuilt by running commands)
│   ├── specs/            118 downloaded API manuals
│   ├── engine.db         the database
│   └── embeddings.npz    the meaning-numbers
│
├── src/engine/
│   ├── config.py         all settings, read from .env
│   ├── db.py             the database tables
│   ├── ids.py            the unguessable ID generator
│   ├── cli.py            the commands you type
│   ├── ingest/           Day 1: downloading and reading API manuals
│   ├── search/           Day 2: the three search methods
│   ├── workflow/         Day 4: the validator and compiler
│   ├── agent/            Day 5: intent parsing and AI planning
│   └── execution/        Day 6: credentials, guards, running, retrying
│
└── web/                  Day 7: the three web pages
│
└── tests/                245 automated checks
```

## What "tests" means and why there are 245

A test is a small program that checks another program still behaves correctly. Ours
verify things like *"a broken spec file produces a clean skip, not a crash"* and
*"`/chat.postMessage` splits into `chat post message`"*.

They run in under a second, so any mistake is caught immediately rather than three days
later. This is the difference between a project that keeps working and one that slowly
rots.

---

# Part 6 — Commands you can run

Open a terminal in the project folder.

```bash
uv run engine stats
```
Shows what is in the database: providers, endpoints, largest APIs.

```bash
uv run engine search "send a message to a channel" --mode bm25
```
Searches. `--mode` can be `bm25` (keywords), `vector` (meaning), or `hybrid` (both).

```bash
uv run engine plan "when a github issue opens, post it to slack"
```
Turns a sentence into a validated workflow. Needs `LLM_API_KEY` in `.env`.

```bash
uv run engine index
```
Rebuilds the keyword index. Needed after importing new APIs.

```bash
uv run engine embed
```
Generates the meaning-numbers. Safe to re-run — it resumes where it left off.

```bash
uv run engine credential add slack_com --secret <your-token>
```
Stores a provider token, encrypted. The value is never displayed again.

```bash
uv run engine run <workflow_id> --dry-run
```
Builds the real request and deliberately does not send it. Drop `--dry-run` to
execute for real.

```bash
uv run engine executions
```
Recent run history.

```bash
uv run pytest -q
```
Runs all 245 checks.

```bash
uv run uvicorn engine.api.main:app --reload
```
Starts the web service. Then open **http://localhost:8000/docs** in your browser for
a clickable page where you can try every function.

> **Note:** if `uv` is "not recognised", add this folder to your system PATH:
> `C:\Users\priya\AppData\Roaming\Python\Python313\Scripts`

---

# Part 7 — Where we are now

| Day | What it does | Status |
|---|---|---|
| 1 | Build the API knowledge base | ✅ 118 APIs, 10,563 endpoints |
| 2 | Search (keyword + meaning + both) | ✅ all three modes working |
| 3 | Measure how good the search is | ✅ hybrid best: NDCG@10 0.412 |
| 4 | **The validator** — the anti-hallucination gate | ✅ 7 checks, 123 tests |
| 5 | The AI planner | ✅ works end to end |
| 6 | Actually running the automations | ✅ real calls, crash-safe |
| 7 | The web page and final results | ✅ built, 6 bugs fixed |

## The build is finished

All seven days are done. What you have:

- A knowledge base of **15,504 endpoints** from **270 real APIs**
- Search in three modes, **measured** rather than assumed, with hybrid best
- A validator that makes invented endpoints structurally unable to run
- An AI planner that turns a sentence into a checked automation in ~20 seconds
- Execution that survives a crash without doing anything twice
- A web interface, and **245 automated tests**

The honest summary of what is *not* built is in the README, alongside the measured
numbers. Both are worth more than a longer feature list.

## Two things for you to do

1. **Rotate your Gemini API key** — it briefly appeared in a local log file. Nothing
   left your machine, but replacing it is quick and removes all doubt.
2. **Put the project in version control.** `git init`, commit, and push. Nearly nine
   thousand lines of working code currently exist in exactly one place.

---

# Changelog

*Newest first. Updated at the end of each working day.*

### After the build — three faults found by one real prompt
- The downloader kept one spec per provider, discarding 280 of Google's 281 distinct
  products; Gmail was among them. Now grouped by provider *and* API title: 676 to
  **2,080** distinct APIs visible, corpus 10,563 to **15,504** endpoints
- The planner prompt instructed the AI to substitute the nearest endpoint when
  nothing fit. It can now answer "this cannot be done" instead
- The intent step was deleting service names before searching, discarding the most
  distinguishing term in the request. Its worked example contradicted its own rule
- Every automation was forced to have an event; a trigger may now be "manual"
- The field summary given to the AI never looked inside lists, so it could not name
  `messages[].id` and declared a possible workflow impossible
- The validator accepted `messages[0].id` while the runner could only read
  `messages[]` — a plan could pass every check and fail at runtime. Both now share
  one pattern, with a test that keeps them aligned
- Made embedding incremental: adding 40 endpoints no longer re-embeds 10,000
- End result: the Gmail-to-WhatsApp workflow compiles and dry-runs successfully
- Added 25 tests (245 total)

### Day 7 — Interface and bug hunt
- Found and fixed six defects, the worst being that unrecoverable 4xx failures were
  retried three times each
- Built three web pages: search, create-with-validation-checklist, and run history
- The browser talks only to the website's own server, never to the engine directly
- Fixed a security vulnerability in the web framework version
- Verified the whole stack working together end to end
- Rewrote the README with measured numbers and an honest built-versus-planned table
- Added 11 tests (217 total)
- After review: fixed a dry run that refused non-allowlisted providers instead of
  reporting them, and a styling rule that glued the method to the address
  (220 tests)

### Day 6 — Execution
- Built encrypted credential storage, the egress guard, the node executor, the
  runner and the background worker
- Proved it live against a real server: two real calls, with the second carrying a
  value taken from the first one's actual reply
- Passed the crash test: re-running a completed execution repeated no HTTP call and
  produced no duplicate side effect
- Detects failures hidden inside 200 OK replies, and retries only the categories
  worth retrying
- Blocked a live attempt to reach the cloud metadata address
- Fixed two of our own bugs: the destination was checked after the secret was
  decrypted, and two parts of the queue used different time conventions
- Added 58 tests (206 total)

### Day 5 — The AI planner
- Built the model adapter (Gemini and OpenAI), intent parsing, and the planner
- Real result: an English sentence became a compiled workflow on the first attempt,
  in ~20 seconds for ~2,500 tokens, and generalised to a different provider pair
- Added the candidate allowlist: the AI may only use endpoints retrieved for this
  goal, and a real endpoint that was not retrieved is still refused
- Built the repair loop that feeds structured validation errors back to the model
- Diagnosed and fixed the model's reasoning consuming its own output budget
- Added 25 tests (148 total), all against a scripted fake model
- Recorded an honest limitation: the demo produced a valid plan that was not quite
  what was asked for, which no amount of validation can catch

### Day 4 — The validator
- Built the workflow model, the schema compatibility checker, and the seven-check
  compiler that is the project's central guarantee
- Verified against the real corpus: the GitHub-to-Slack workflow compiles, and
  invented endpoints, wrong field names, type mismatches and ungated destructive
  actions are each refused with a precise reason
- Added 74 tests (123 total), each broken plan asserting its specific rejection
- Corrected our own over-strict rule that had classified OAuth 2.0 as unusable
- Added workflow storage, versioned by insert, and API routes for save/validate

### Day 3 — Measurement
- Wrote 30 labelled questions with known-correct answers, found by database lookup
  rather than by our own search engine
- Built a scoring program measuring P@5, R@20, MRR, NDCG@10 and speed
- Result: combining both search methods wins on every quality measure
- Settled the Day 2 synonym question with data — synonyms help, and the single
  example that suggested otherwise was misleading
- Tested making synonyms symmetric: theoretically right, measurably worse, reverted
- Documented three questions no method answers, and chose not to paper over them
- Recorded that hybrid search slightly exceeds the 500 ms target at p95

### Day 2 — Search
- Built keyword search (BM25) over all 10,563 endpoints — correct answer ranked #1 in ~13 ms
- Added address-splitting, which turned out to be essential for Slack
- Built meaning-based search and the method for combining both
- Created a web service with 4 functions and automatic documentation
- Added 31 tests (total: 49)
- Found and fixed a security mistake: an API key reaching a log file
- Discovered Google's free tier caps embeddings at 1,000/day and switched to local,
  unlimited, reproducible embeddings
- Recorded an honest negative result about synonyms rather than hiding it
- Finished generating all 10,563 meaning-vectors locally (~45 min, free, no quota)
- Confirmed meaning-search fixes a real keyword failure (Stripe payments) and that
  combining both methods helps on some questions and hurts on others
- Measured the speed cost of meaning-search: ~350 ms versus ~13 ms for keywords

### Day 1 — Knowledge base
- Downloaded 118 API manuals from APIs.guru
- Built a parser handling both spec formats, circular references, and broken files
- Imported 10,563 endpoints with their fields, types, and login requirements
- Verified importing twice creates no duplicates
- Wrote 18 tests
- Fixed: duplicate GitHub specs, a cap that removed our demo endpoint, and slow
  downloading (15 min → 40 s)

### Day 0 — Planning
- Wrote the design documents
- Chose the technology (all Python, plus Next.js for the future web page)
- Made a realistic 7-day plan, cutting a 3-month design down to what fits
