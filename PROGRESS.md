# PROGRESS.md

Append-only log of decisions and assumptions. The brief says a logged assumption
counts in favour; an unlogged one is invisible. Newest at the bottom.

---

## 2026-08-06 — Round scoped, docs read, repo scaffolded

**Read:** the full 14-page task document, `docs.superdocs.app` index,
CONTRIBUTING.md of `superdocsapp/superdocs-builds`, and the API pages for curl
examples, HITL, multi-document sessions, MCP tools, agent signup, and billing.

### Decision — domain is vendor contracts

MSAs, amendments, SOWs, invoices, renewal notices. Chosen over insurance claims
and loan files because the disagreements are **numeric and provable** — a rate
in the MSA versus the same rate in amendment 2 versus what the invoice actually
billed. That makes "the system surfaces the conflict" demonstrable in a
three-minute video without the viewer needing domain vocabulary. Everything is
fabricated; fictional counterparties only.

**Deliverable:** an obligations-and-commercials register. One row per obligation
or commercial term, every cell citing the exact source span it came from.

### Assumption — "a second run with different documents"

The brief asks whether the system works a second time on different documents.
Reading that as: the same declared format and domain set, different files. So
two full corpora ship — `corpora/pile_acme/` and `corpora/pile_northwind/` —
with different counterparties, different clause phrasings, and a different
conflict profile. The README declares the accepted formats and domain explicitly.

### Assumption — the watched location is a local directory

The brief says "new documents keep arriving into a watched location" without
naming a transport. Implementing as a watched local directory, with the ingest
path behind an interface so an S3 or mailbox source is a config change. Rationale:
a stranger cloning the repo can drop a file into a folder and watch the update
happen; anything cloud-backed needs credentials they do not have.

### Decision — LangGraph with a Postgres checkpointer

Behavior 2 (resume after being killed) becomes a property of the orchestration
layer rather than bespoke state-machine code that has to be argued for in the
write-up. The stack the brief names is Python + FastAPI + LangGraph + Postgres
with vector search + React, and matching it is worth more here than being clever,
because they say explicitly that building in their stack is how they compare the
work to the job.

### Deadline — 20 August 2026

Confirmed by Vinayak on 7 Aug. Thirteen days, targeting done on the 18th with two
days of buffer. Day-by-day schedule and the pre-declared cut list are in
[PLAN.md](PLAN.md).

---

## API facts worth not re-deriving

Base URL `https://api.superdocs.app`, bearer auth with an `sk_` key.

The four-call minimum contract:

| Step    | REST                                      | MCP tool                 |
|---------|-------------------------------------------|--------------------------|
| upload  | `POST /v1/documents/upload`               | `upload_document_base64` |
| chat    | `POST /v1/chat`, `POST /v1/chat/async`    | `chat`, `chat_async`     |
| approve | `POST /v1/chat/{session_id}/approve`      | `approve_change`         |
| export  | `POST /v1/documents/export`               | `export_document`        |

- Set `approval_mode: "ask_every_time"` and the job parks at
  `status: "awaiting_approval"` instead of applying.
- A proposed change is `{change_id, operation, chunk_id, old_html, new_html,
  ai_explanation, insert_after_chunk_id}`. `operation` is edit | create | delete.
- Batch approve with mixed decisions is allowed, but `approved` must **also**
  appear at the top level — it is the default for entries that omit their own.
- The **double-parse trap is narrower than the brief states** — measured, see the
  2026-08-07 entry. `metadata.pending_changes` on the polling path is already a
  parsed list. The JSON-encoded string lives in
  `intermediate_responses[].content` where `type == "proposed_change_batch"`,
  i.e. the streaming path. Integrators seeing `undefined` in every diff field are
  on SSE, not polling.
- Uploads parse into HTML with **chunk IDs**; chunk-level editing is what makes
  "changed exactly this and nothing else" provable.
- **Multi-document sessions are real.** `open_mode` = replace | new_focused |
  background. One edit turn can touch several open documents;
  `proposed_change_batch` groups changes by `document_id`. This is the primitive
  the assigned build needs — see the Task 2 note below.
- `upload_image_base64` returns a stable URL for `<img src>`.
- Templates persist via `upload_template_base64` / `list_user_templates`.
- Deep operations run 30s to several minutes with no visible progress. That is
  still processing, not a crash. SSE and `get_job` polling both exist.
- Free tier: 500 operations/month. One operation covers up to 25 sections edited
  in one request. Exports, downloads and denied changes cost nothing. Searches do.
  Over quota returns 429.

### Task 2 design consequence

The assigned build is graded on three things, and the API has a primitive for
each:

1. *Quick-start stays consistent when the manual changes* → open manual and
   quick-start card in **one multi-document session**, apply the edit as a single
   turn, take the grouped `proposed_change_batch`. The two documents cannot
   drift, because one instruction produced both edits.
2. *Diagrams regenerate on edit rather than being re-drawn* → the exploded parts
   diagram and the per-step diagrams render **deterministically from the parts
   graph** as SVG. On edit, re-render from the graph, `upload_image_base64`, swap
   one `<img src>` in one chunk. Nothing is ever redrawn by a model.
3. *Localized versions keep the same page structure* → save the manual skeleton
   with `upload_template_base64`, fill per-locale content into the fixed
   skeleton, then assert section count and page-break positions match the source
   locale. Assert, not claim.

Single source of truth is a structured product spec (product, parts graph, steps,
warnings, specs). The manual and the quick-start card are two projections of it.

### Blocked on the human

`POST /v1/agents/signup` requires `terms_accepted: true`, which is account
creation and terms acceptance. Vinayak runs that himself and provides the key via
`.env`; the agent does not create accounts or accept terms on his behalf. The
SuperDocs docs ask for the same confirmation.

*Resolved 2026-08-07:* key created through the web dashboard rather than agent
signup, so `is_agent_account: false`. Promo code redeemed — "Round 2 Builders -
August 2026", 10,000 operations, expiring 5 Sept 2026. Operations are not a
constraint on this build.

---

## 2026-08-07 — Four-call spine verified against the live API

Ran the upload → chat → approve → export contract end to end on a fabricated
MSA. Every claim below is measured, not read.

### It works, and the human gate is genuinely surgical

Uploaded a nine-chunk MSA, asked for two edits in one turn with
`approval_mode: "ask_every_time"`, **approved one and denied the other**, then
exported. Result:

| Check | Outcome |
|---|---|
| Approved edit applied (rate → USD 135) | PASS |
| Denied edit rejected (still 30 days, not 45) | PASS |
| Term clause untouched | PASS |
| Termination clause untouched | PASS |
| No sections added or dropped | PASS |

Rejecting one change did not discard the other. That is the exact shape the
Task 1 human gate has to reproduce, now confirmed to be achievable rather than
assumed.

### Measured API facts

- **Upload** returns parsed HTML with a `data-chunk-id` UUID on **every element**,
  plus `chunks_count`, `version_id`, `document_id`, and a `documents[]` roster.
  Chunk IDs are the addressing scheme for provable targeted edits.
- **`chat/async`** returns `{job_id, session_id, status: "pending"}` immediately.
  Poll `GET /v1/jobs/{job_id}`. This run reached `awaiting_approval` in ~18s
  across four polls; `progress` sat at 88.
- A pending change is exactly
  `{change_id, operation, chunk_id, document_id, old_html, new_html, ai_explanation}`.
  Note `document_id` is present per change — that is what makes cross-document
  batching work for the Task 2 build.
- **Approve** with `{job_id, approved: <default>, changes: [{change_id, approved}]}`
  returns `{status, message, batch_complete}`.
- **Export** `format` must be one of `docx | pdf | html | markdown | txt | doc`.
  `"md"` is rejected with a 422. Export returns **raw bytes, not JSON**, and
  strips `data-chunk-id` — it is a styled presentation artifact. To verify
  surgical precision, compare the session document state, not the export.
- `metadata.intermediate_responses[]` is a running narration of the stages the
  editor moved through ("searching for the Fees section", "processing 2 sections
  in parallel"). `metadata.cumulative_tokens` is also present — a usable model
  for the cost-reporting behavior in Task 1.
- Session history exposes `checkpoint_id` per turn, and there is a rewind
  endpoint. Their own resumability primitive.

### Rough edges to report (Task 4, question 1)

1. **`/v1/agents/whoami` quota is wrong and omits promo credits.** After a chat
   operation that produced two changes, it still reported
   `{used: 0, remaining: 500}`, while the billing UI showed `1 / 500`. It also
   makes no mention of the 10,000-operation promo. An agent budgeting its own
   spend from `whoami` — the exact use the docs recommend — would be flying blind.
   Reproducible in three calls.
2. **`/v1/promo/promotions` 404s.** The docs index lists "List My Promotions" but
   the real path is not this one. Path to be confirmed before reporting, so this
   may be my guess rather than their bug.
3. `/v1/documents/{durable_document_id}` returns 200 while
   `/v1/documents/{durable_document_id}/detail` 404s. Minor, possibly intended.
4. Docs describe the double-parse trap as applying to pending changes generally;
   it applies only to the streaming path. Worth telling them — it is a
   documentation fix that saves integrators real time.

Probe scripts are throwaway and live outside the repo; they are not deliverables.

---

## 2026-08-07 — Foundation built (PLAN.md phase "Aug 7–8")

Schema, migrations, config loader, recorded LLM provider, ingest, HTTP surface.
**35 tests, all green, with `ANTHROPIC_API_KEY` and `SUPERDOCS_API_KEY` scrubbed
from the environment.** `docker compose up` goes from a destroyed volume to a
healthy API serving the seeded pile in about four seconds.

### Two bugs found by the tests, both mine, one that mattered

**Precedence was inverted *and* domain-wrong.** `len(order) - index` ranked
earlier entries higher — the opposite of what the config comment promised. But
fixing the arithmetic alone would have left the worse bug: I had ordered
`[msa, sow, amendment, notice, invoice]`, putting invoices at the top. An
invoice is evidence of what *was* billed, never authority on what the rate
*should have been*; ranking it highest would have made the system propose the
billing error as the correct answer, in exactly the scenario this pile is built
to catch. Reordered by contractual authority:
`[invoice, notice, sow, msa, amendment]`. There is now a test named for the bug.

**Line wrapping breaks naive span matching.** "USD 120 per hour" wraps across a
newline in the MSA, so it is not a contiguous substring of the extracted text.
Deliberately *not* fixed by reflowing the corpus — real documents wrap, and
reflowing would shift every offset and corrupt provenance. The burden belongs on
the span matcher, which must search whitespace-tolerantly. Pinned by
`test_extraction_preserves_line_wrapping_rather_than_reflowing` so it cannot be
forgotten when extraction lands.

### Decisions

- **`uv`, not `python3-venv`.** `ensurepip` is absent on this box and installing
  `python3.12-venv` needs sudo. `uv` was already present and is what the crawler
  repo uses. No system change required to build this project.
- **Ports 5434 and 8010**, not 5432 and 8000 — both defaults were already bound
  on this machine (5433 too, by the crawler). Overridable via `DB_PORT` and
  `API_PORT`. Worth catching now: a stranger cloning this repo hits the same
  collision, and behaviour 6 is graded.
- **Plain psycopg3 and SQL files, no ORM and no Alembic.** LangGraph's
  checkpointer speaks psycopg, so the system needs exactly one driver, and a
  reviewer can read the entire data model in one file.
- **`ON CONFLICT DO NOTHING` then re-select** in the document insert. Losing the
  race to a concurrent ingest of identical bytes is a correct outcome, not an
  error — early groundwork for behaviour 9.

### Verified, not assumed

| Claim | How |
|---|---|
| Re-ingest changes nothing | Row-level before/after comparison, not just "no duplicate" |
| Content-addressed, not name-addressed | Same bytes under a new filename → same `document_id`, over HTTP |
| Unsupported formats become visible gaps | `.xlsx` and a corrupt PDF both land as `status='unsupported'` with a note |
| Poisoned document ingests as ordinary data | Instruction text preserved verbatim so it can be *reported on* later |
| Suite needs no key | Run with secrets scrubbed from the environment |
| Cold start works | `docker compose down -v` then `up` → healthy in ~4s |

### Still open

- `corpora/pile_northwind/` is empty; the second corpus is scheduled for Aug 18.
  Seeding skips it gracefully rather than failing.
- The `api` service has no MCP counterpart yet. Behaviour 4 requires both
  surfaces to expose the same operations, including approval.

---

## 2026-08-07 (later) — UNDERSTAND stages built, 126 tests

Five commits, each self-contained: `foundation`, `spans`, `normalize`,
`screen`, `classify + extract`. All offline.

### Decisions

**A fact whose quote cannot be located is discarded.** Not kept with a weaker
citation, not pointed at the whole page — discarded, and recorded as a gap. This
throws away values that are probably correct. It is the right trade: a value
that is probably correct and definitely uncitable is exactly what an ungrounded
register is made of, and grounding is the entire claim of this system.

**The poisoned document never reaches the model.** The deterministic screen runs
*before* the classify call and short-circuits it. Spending a call interpreting a
document we already know is trying to manipulate the interpreter would give the
manipulation one chance to work. Asserted by a test that requires zero model
calls. The model still gets an independent vote on injection afterwards, and
either layer firing quarantines — regex can be evaded by novel phrasing, a model
can be talked out of its judgement, and they do not fail together.

**Two provider doubles, for two different jobs.** `FakeProvider` replays
recordings of real model output, for end-to-end runs. `ScriptedProvider` (tests
only) drives stages through responses that are awkward to obtain on demand:
malformed JSON, an invented quote, a type outside the taxonomy, low confidence.
Those branches are in *our* code, so testing them this way is not "proving the
mock works" — but the end-to-end path deliberately does not use it.

### Discovered while building

- **Whitespace tolerance is not an edge case, it is the common case.** The
  single most important value in the pile — the MSA's `USD 120 per hour` — wraps
  across a line and is unfindable by `str.index`. Without the whitespace pass in
  `spans.py`, the demo's central conflict would silently never be detected. My
  own prompt strings hit the same trap in a test assertion an hour later.
- **`Decimal.normalize()` produces scientific notation.** `Decimal("160")`
  becomes `1.6E+2`, which would have put exponents in register cells and made
  two spellings of one quantity compare unequal as strings. Fixed with a
  fixed-point formatter.
- **False positives are the real risk in injection screening, not misses.**
  Contracts are dense with imperatives, approval claims and no-issue assertions —
  the exact vocabulary the patterns hunt. Every pattern now requires a
  system-referent rather than an imperative mood, and the suite asserts all seven
  corpus documents plus eight legitimate clauses come back clean. A reviewer who
  sees false quarantines stops reading them, and then the real one goes through.

### Blocked — *resolved same day, no paid key needed*

The fixtures needed real model output and `.env` had no model key. Rather than
take a paid dependency: OpenRouter carries **17 models priced at zero**, and
recording is a one-time act. Recorded the whole Acme pile on
`nvidia/nemotron-3-ultra-550b-a55b:free` for **$0.0000** — 7/7 classified
correctly, 48 facts, 0 gaps. Afterwards everything replays with no key and no
network, which is what behaviour 7 actually asks for.

No signup anywhere new; the existing OpenRouter key was enough, and the free
tier rate-limits rather than bills, so the recorder backs off on 429 instead of
failing. Anyone reproducing the repo can either replay the committed fixtures
(no key at all) or re-record on the same free model.

**Real model output validated a speculative design choice.** Five of the 48
facts matched only after whitespace normalisation — including the MSA's
`USD 120 per hour`, the single most important value in the pile. Until this run,
the whitespace pass in `spans.py` was a defensible guess. It is now the thing
standing between the demo and a silently missing central conflict.

`Completion.json()` gained a balanced-brace fallback, because smaller models
narrate before answering. Scoped so it still raises on a refusal or an error
message — a parser that always finds something would turn those into a silently
empty result.

---

## 2026-08-07 (evening) — the first grounded register exists

`python -m scripts.run_pile --pile pile_acme --out register.md`, offline, no key:
48 facts, 0 gaps, **3 conflicts**, every cell citing a real span.

### The design bug the first real run exposed

Grouping every field by `(entity_key, field)` produced **nine** conflicts, six of
them noise: three invoices legitimately carry three invoice numbers, three dates,
three amounts. That is precisely the failure the screening rails were built to
avoid, arriving through a different door — six false conflicts alongside three
real ones is worse than no detection at all, because the reviewer stops reading.

Fix is config, not code: `instance_fields` declares which fields describe the
document instance rather than the engagement. `hourly_rate` and
`payment_terms_days` are deliberately *absent* from that list, because an invoice
restating them differently is exactly the finding worth having. Config validation
now rejects a typo in the list, since a misspelt field silently re-enables the
noise and nobody notices until a demo.

Result: exactly the three planted disagreements, each with the right proposal —
the amendment's rate over the MSA's and the stale invoice's, the raised liability
cap, and invoice 1043's Net 45 against the agreement's 30 days.

### Two display bugs

Text values casefold for comparison, and that form was being rendered into a
document a human reads (`acme fabrication services llc`). `display` is now
separate from `canonical`: machines group on one, people read the other. And the
markdown table separator row was one character wider than its header, misaligning
every table in the deliverable.

### Proven, not asserted

- **Determinism.** Identical facts render identical bytes across runs.
- **Localised change.** Editing one fact changes only the hash of the section
  holding it. This is the property the whole "an update should cost like an
  update" claim will be built on, and it is now a test rather than an intention.
- **Honest zero.** A pile whose facts all agree renders "No disagreements found",
  and the no-findings path has its own test.
- **Cost by stage.** `report.cost_by_stage()` gives calls, ms and tokens per
  stage — behaviour 10 falling out of the same record that makes stages
  watchable (behaviour 1).

---

## 2026-08-07 (night) — the gate holds; behaviours 3 and 4 done

Persistence, the human gate, and the HTTP surface over both. Drove the entire
flow with curl — start, review, decide, commit, report — with no UI involved,
which is behaviour 4 satisfied by construction rather than retrofitted.

### The failure shape worth remembering

Ran the container before its recordings shipped in the image. All seven
documents *escalated*, and the run reported **success with zero facts**. It
looked healthy. Nothing errored.

The cause: `MissingFixture` inherited from `ProviderError`, and the stages catch
`ProviderError` to mean "the model gave an unusable answer, send it to a human".
But a missing recording does not mean the document was hard to read — it means
the deployment cannot reach a model at all. Those are different failures and
they must not share a code path.

Now split: `ProviderError` is unusable output (stages escalate, which is
correct — a person should look at that document), `ProviderUnavailable` is
unreachable (propagates and fails loudly). A broken deployment that looks
healthy is the worst available failure shape, and it is exactly what behaviour 5
forbids: a success message that does not mean the output is in the state it
claims.

Also moved recordings from `tests/fixtures/` to `recordings/`. They are not test
data — they are what lets the demo and the container run with no key and no
network, so they ship with the application.

### Verified over HTTP, not asserted

| Claim | Evidence |
|---|---|
| Nothing exists before review | `GET /register` → 404 while the run is open |
| Cannot commit while undecided | `POST /commit` → 409, "9 proposals still pending" |
| Per-item review works | Rejected the rate conflict, approved 8 others, in one call |
| Rejection is respected | `hourly_rate` → rejected; `liability_cap`, `payment_terms_days` → approved |
| Audit answers the question | 6 rows: section, from-hash, to-hash, run, timestamp |
| Cost is reportable | `GET /report` — calls, ms, tokens and paths per stage |

**Determinism holds across environments.** The section hashes the container
committed (`f2094b8a`, `d87f2b4f`, `fdfdcea8`, `88ff8462`, `5695b9a8`,
`fe71da09`) are byte-identical to those from a local venv run. That is the
foundation the "an update touched nothing else" proof stands on, and it now
holds across processes and machines rather than only within one.

---

## 2026-08-07 (late) — the third movement: it stays alive

`amendment_02.md` arrives, raising the rate to USD 145 and the notice period to
90 days. Result: **2 model calls, 4 sections changed, 2 provably byte-identical,
7 proposals raised instead of 9.**

### Two economies, deliberately not conflated

Model work is genuinely incremental — only the arriving document is classified
and extracted. Composition is recomputed *in full* and then diffed, because it is
pure CPU over facts already held. That is the counter-intuitive half and it is
the point: recomputing makes the untouched sections **provably** unchanged rather
than unchanged because we predicted they would be and skipped them. A predicted
no-op is an assumption; a recomputed identical hash is evidence.

### Two bugs the arriving document exposed

**Entity resolution cannot trust the model for identity.** Amendment 1 answered
`Acme Fabrication Services LLC`. Amendment 2 — a near-identical document —
answered `Brightwell Manufacturing Inc. and Acme Fabrication Services LLC`. Both
are defensible readings of "who is the agreement with", and they slug to
different keys.

The consequence would have been silent and total: the pile splits into two
engagements, no group ever holds both rates, and the contradiction the update
exists to surface is invisible — while the register looks perfectly clean. This
is the most dangerous failure the system can have, because it produces
confident-looking output with nothing behind it.

Resolution now treats the model's answer as a *candidate name* and settles
identity against engagements the pile already knows: alias → exact → token
containment → new. A name matching two known engagements **escalates**; merging
corrupts the register, splitting hides every conflict, and neither is ours to
choose silently.

**Precedence by document type could not separate two amendments.** Both outrank
the agreement they amend, so the proposer correctly refused to choose — and thus
gave up exactly where a reviewer most needs an answer. It now breaks the tie the
way the documents themselves do: the later effective date supersedes the earlier,
stated explicitly in the rationale. Where dates are missing or equal it still
refuses to guess.

### Proven

| Claim | Evidence |
|---|---|
| An update costs like an update | 2 model calls, not 16 |
| Untouched sections unchanged | `billing`, `gaps` — hashes recomputed and identical |
| Something *did* change | `commercials`, `risk`, `parties`, `disagreements` moved |
| Only the update is proposed | 7 proposals, `billing` not among them |
| New contradiction surfaced | USD 145 vs 135 vs 120, proposed from amendment 2 |
| Re-arrival is a no-op | duplicate → 0 model calls, 0 new facts |
| Version 2 is complete | 4 written + 2 carried = 6 sections |
| Audit names the cause | `commercials d87f2b4f → 96ec3a2b, cause=amendment_02.md` |

---

## 2026-08-07 (later) — the port, and behaviour 2

PLAN.md had committed to LangGraph in four places and the code did not contain
it. This is the straight port: `StateGraph`, `PostgresSaver`, `interrupt()` at
the gate. Not one stage changed — they were written as pure functions taking
explicit arguments so this would be a wrap, and it was.

**The evidence the port changed nothing it understood.** The six section hashes
of `pile_acme` come out byte-identical to the ones the hand-rolled pipeline
committed (`f2094b8a`, `d87f2b4f`, `fdfdcea8`, `88ff8462`, `5695b9a8`,
`fe71da09`) — across a different orchestrator, facts reloaded from the database
instead of held in memory, and an entity resolver that now runs on both paths.

### The decision that mattered was not the framework

Adopting LangGraph gets you a checkpoint. It does not get you behaviour 2,
because the checkpoint and the work are two writes and both orderings lose:

| | |
|---|---|
| Work committed, checkpoint not | Resume replays the node. Facts twice, stage events twice, the cost report overstates what the pile cost. |
| Checkpoint committed, work not | Resume skips a node whose output never landed. A register that claims to be complete and is not. |

Both are silent, both report success, and the second is precisely what
behaviour 5 forbids. So there is exactly one write: the checkpointer shares the
run's connection and commits it when it persists the step, which puts the
node's work and the record that the node finished in one Postgres transaction.

**LangGraph's default durability is `async`** — the checkpoint written on a
background thread while the next step already runs. That is the losing ordering,
shipped as the default. It also happens to be caught loudly rather than quietly
here, because two threads cannot share one psycopg connection: the first attempt
died on `another command is already in progress`. Runs are driven with
`durability="sync"`.

### The model-call ledger

Checkpointing still leaves the node that was in flight re-running from the top,
which is correct — a half-run node is not finished work. But re-issuing its
model call means the run pays twice for one answer, and then the cost report
lies in the expensive direction.

So `model_call` records every completion under `(run_id, call_key)` and commits
it **immediately, on its own connection, outside the run's transaction**. The
ordering is the whole point: keeping a recorded answer through a rollback is
safe, because the key is the prompt and the same question has the same answer.
Losing it is what costs money. It is keyed per run, not globally — a fresh run
genuinely re-asks, because resumption is a promise about one run.

### Two orchestrators became one

The full run and the incremental update were separate code paths, and that is
how the entity-resolution fix came to live on one and not the other. They are
now the same graph with different starting conditions. Three things fell out of
sharing a gate:

- Conflicts are re-proposed when what they **say** changed, not when the arriving
  document happened to mention the field. The old rule guessed at which
  conflicts could have moved; this one compares the values.
- Sections got the same rule, by content hash — which caught a bug the tests
  found: a second run arriving while the first was still at the gate re-proposed
  every section already on the reviewer's desk.
- Commit refuses to write a section whose hash differs from the one that was
  approved. That is what lets the register be rebuilt from a checkpoint, and it
  deleted the API's `_REGISTERS` dict, which used to answer `POST /commit` after
  a restart with *"re-run to recompose it"* — a server telling a caller to redo
  finished work because the server forgot.

### Proven, by killing a real process

Every test sends a real `SIGKILL` and asserts the child's returncode is `-9`. A
child that exited cleanly did not test this.

**Killed inside the third extraction**, after its answer was bought and recorded
but before the node committed:

| | After the kill | After resume |
|---|---|---|
| facts | 13 | 48 |
| classify / extract events | 3 / 2 | 7 / 7 |
| answers bought | 6 | 14 total, 1 replayed |
| register | — | byte-identical to a control run |

Five finished calls untouched, the one in flight replayed, the remaining eight
bought once.

**Killed before the sixth call**, nothing bought yet: 5 in the ledger before, 14
after. A run redoing completed calls would show a ledger larger than 14.

**Killed at the gate**: a fresh process that has never seen the register commits
it, and the stored hashes match the ones reviewed.

Also proven: a crash commits no deliverable and no audit row; no stage is
recorded twice; what the run reports spending equals what it actually bought;
nine proposals stay nine. Both mechanisms were mutation-checked — disabling the
ledger fails the replay test, removing the checkpointer commit fails the
gate-survival test. Worth recording: the commit in `put` is redundant today
because `put_writes` lands first and carries the node's work with it. It stays
as defence against LangGraph's ordering changing, not against a live window.

### What this cost elsewhere

Rollback-per-test cannot isolate a run whose whole point is that it commits, so
piles are unique per test and dropped at the end of the session. `understand.py`
and `incremental.py` are gone, and `test_reconcile_compose.py` now needs the
database — a second, database-free orchestrator kept for test convenience is
exactly how the identity fix went missing from one path.

Also pinned `LANGSMITH_TRACING=false`. LangGraph brings langsmith, which traces
to a hosted service when a flag says so. Off by default is one stray environment
variable away from a suite that reaches the network with document text in the
payload, and behaviour 7 deserves better than an assumption.

---

## PICK UP HERE

### State as of 2026-08-07

22 commits, 205 tests green offline, $0.00 spent. Behaviours 1, 2, 3, 5, 6, 7, 8,
10 done; 4 partial (HTTP yes, MCP no); 9 not started. The orchestration decision
is settled: straight LangGraph port, done, and the reasoning that mattered is in
the entry above.

### Remaining, in order

1. **Behaviour 9 — concurrency.** Two runs at once on the same pile stay two
   runs. `advisory_lock` exists in `store/engine.py` and is still unused. One
   thing already known: it uses `pg_advisory_xact_lock`, which is
   transaction-scoped, and a run's transaction now closes at every checkpoint —
   so the lock a run needs is the session-scoped `pg_advisory_lock`, taken on
   the connection the run owns for its whole life. LangGraph's checkpointing is
   per-thread and does not help here; the contention is at the section level and
   the lock lives in our schema, as PLAN.md §6 predicted.
2. **MCP server** → completes behaviour 4 in the shape the brief calls
   strongest. Every operation already exists in `app/api/runs.py`, including
   `resume`, so this is a second surface over the same calls rather than new
   behaviour.
3. **The EXAMINE stage** against `rules/playbook.yaml` — the second of the three
   movements and the largest untouched gap. Nothing consumes the playbook yet.
   It slots in as a node between `compose` and `delta`, and its findings become
   proposals like everything else.
4. React review UI (degradable to a minimal table; first item on the cut list).
5. Second corpus `pile_northwind` — currently empty; seeding skips it gracefully.
6. Tasks 2, 3, 4.

### Notes for whoever picks this up

- The graph is `app/graph/build.py`; read `checkpoint.py` and `state.py` first,
  they carry the two decisions everything else follows from.
- Nodes never call `commit()`. If a new node does, it has broken behaviour 2.
- A node may run twice. Anything a node writes must be safe to write again, or
  it belongs before the interrupt rather than after it — which is why `gate`
  writes nothing and `propose` exists separately.
- State is plain JSON on purpose. If something needs a custom serialiser to
  cross a node boundary, load it from the database instead.
