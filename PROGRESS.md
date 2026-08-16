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

## 2026-08-07 (later still) — behaviours 9 and 4

### Concurrency: the danger was not the crash

Measured by removing the lock and running two real processes at one pile: the
reviewer gets **fifteen proposals where there are nine**, with nothing erroring.
The loud failures were easier to find and less dangerous than that one.

The lock is session-scoped, and that is forced rather than preferred. A run's
transaction now closes at every checkpoint, so a transaction-scoped lock would be
dropped and retaken between every pair of stages — absent exactly when a second
run is likeliest to slip through. Session scope also gets the failure case right
for free: a killed process releases the pile when its socket closes, so
behaviours 2 and 9 can hold at once. A dead run holding a pile against the
process sent to resume it would break both.

**The lock is not held across the gate.** A reviewer may take a week. The run
gives the pile back when it halts and takes it again when resumed to commit,
which is safe only because the commit-time hash check refuses approved bytes
that no longer match.

### Two bugs the contention test found, both worth more than the lock

**The lock release was eating every error under it.** A failed run leaves its
transaction aborted, so the unlock in the `finally` raised
`InFailedSqlTransaction` and *replaced* the exception that actually killed the
run. The first version of this test reported a transaction-state complaint and
hid a `UniqueViolation` underneath it. Now it rolls back first — safe, a session
lock does not belong to a transaction — and swallows whatever is left, because
closing the connection releases the lock anyway.

**Ingest reported a lost race as a fresh ingest.** With the real error visible:
two overlapping ingests died on `page (document_id, page_no)`. The loser found
the winner's document, was told nothing about having lost, and went on to insert
pages for it. Fixed in ingest rather than hidden behind the lock, because
`POST /piles/{id}/documents` has the same race with two clients uploading
identical bytes.

**And a claim I had to withdraw.** An earlier draft of the locking docstring said
concurrent runs produce ninety-six facts and a register that looks fine. Running
it showed that does not happen in this code. Corrected to the measured failure
modes — writing the confident version would have been exactly what this repo is
supposed to be against.

### The machine surface, and who is on the other side of the gate

Everything moved into `app/operations.py` before MCP was written. HTTP and MCP
are now argument parsing and error mapping with no decision in either, and there
are tests for both halves of that: every operation has a tool, and neither
surface reaches past `operations` into the machinery. The reason is on the
record already — a full run and an update were two code paths, and the identity
fix landed on one. Two *review* surfaces failing that way would be worse,
because the drift would be about who may approve what.

Behaviour 3 says a person holds the gate. Behaviour 4 says a machine must be
able to drive approval. Refusing to expose `decide` fails one; exposing it
quietly makes "a human reviewed this" unfalsifiable. The answer taken: accept
both and make the record unable to blur them. `decided_by` is who the caller
names; `decided_via` is written by the surface and cannot be claimed. `decided_by`
is now required with no default anywhere.

What an agent still cannot do, structurally: commit anything unproposed, or
commit content that differs from what was approved.

Found while wiring MCP: `pipeline.resume` raises a bare `LookupError` for an
unknown run and neither surface translated it, so resuming a nonexistent run
would have been a 500 over HTTP rather than a 404. Converted once, in
operations, where the pipeline's vocabulary stops.

### Verified against the container, not only the suite

`docker compose up`, then: seven documents, 48 facts, 3 conflicts, 9 proposals,
the same six section hashes as every other run. Rejected the rate conflict and
approved eight others in one call. **Restarted the API container**, then
committed — six sections written, six audit rows. The register survives the
process that composed it, which is the `_REGISTERS` hole closed and proven over
HTTP rather than in a unit test.

Two simultaneous `POST /runs` on one pile both returned 200: the second waited
out the two-second window, found everything already ingested and proposed, and
reported `no_change` with zero proposals. Two runs stayed two runs.

---

## 2026-08-07 (last) — the second movement: it examines the pile

The playbook had sat in `config/` since the first commit with nothing consuming
it. EXAMINE is now a graph node between `compose` and `delta`.

### The decision that made it honest

**A rule has three outcomes, not two.** `violated`, `satisfied`, and
`not_enough_evidence`. The third is the one that makes the other two worth
reading: a rule the pile could not answer is not a rule the pile passed, and
collapsing them is how a report comes to say a contract is clean when what
actually happened is that nobody could tell. A pile with no facts reports "no
rule could be judged", not "no violations".

Against `pile_acme`: 4 violated, 3 satisfied, 1 unjudgeable.

| Rule | What it found |
|---|---|
| `hours_within_sow_cap` | 460 hours billed against a 400 cap, over by 60 |
| `rate_matches_current_agreement` | amendment governs at USD 135, invoice billed 120 |
| `payment_terms_consistent` | MSA governs at 30 days, the invoice states 45 |
| `termination_notice_observed` | MSA requires 60 days, the notice gave 30 |

### No model is called, on purpose

"Invoice 1043 billed 40 hours against a cap of 35" is subtraction. Asking a
model turns a citable, always-correct finding into an occasionally-wrong one a
reviewer has to check — and this stage runs on every arrival, so it also costs
nothing to keep it that way. There is a test asserting the stage spends zero
tokens. The place a model would genuinely earn its keep is a rule that needs
reading rather than counting; that is the `judged` kind the vocabulary does not
have yet, named in the module so its absence is a decision rather than a gap.

### Where configuration-over-code actually stops

A rule is a block in `playbook.yaml` naming one of six check kinds. Adding a
rule shaped like an existing one is YAML and nothing else — there is a test that
adds one and gets a cited finding without touching Python. Adding a new kind of
arithmetic *is* Python. Stating that boundary is better than claiming the
stronger version and being caught by the first reviewer who tries.

A mistyped check kind or a missing parameter fails when the domain loads. A rule
that quietly stops being enforced is worse than no rule at all.

### Two things the first version got wrong

**It accused the MSA of violating the amendment that amended it.** That is not a
finding — it is the definition of an amendment. Found by reading the output
rather than by a test, which is the argument for looking at what a stage
actually says before believing it works. `must_match` now names the document
types genuinely bound to comply.

**It treated equal authority disagreeing as a breach.** Two documents of the
same rank stating different values is unsettled, and naming one of them the
governing value here would silently resolve exactly what the gate exists to keep
open. It now reports that the question belongs to the conflict, not to the rule.

`amendment_references_parent` is kept and reports that it cannot be judged: no
extraction schema declares the field, so nothing in the pile could answer it
however the contracts are written. That is a gap in *our* configuration, and
calling it a violation would blame the documents for our omission. Adding
`amends_agreement` to `extraction/amendment.yaml` is what would make it live —
and that changes a prompt, so it would need fixtures re-recorded.

`no_instructions_in_sources` is where behaviour 8 stops being a branch in the
pipeline and becomes something a reviewer reads: quarantine already kept the
document out of every input, and the playbook states it and quotes the text
back.

### Proven

Both load-bearing properties were mutation-checked rather than trusted:
collapsing `not_enough_evidence` into `satisfied` fails five tests, and ignoring
`must_match` fails the superseded-document and clean-pile tests. Findings reach
the gate as their own items — violations only, because a rule that passed is
something to read and not something to approve — and a finding whose text has
not changed is never re-asked. Verified over HTTP against the container, with
citations and character offsets on every violation.

---

## The review UI, and the four things it found

Vite + React under `web/`, built in a node stage and copied into the Python
image, served at `/review/` from the same origin as the API. Cut-list item 1,
now built.

### It is a client, not a surface

`web/src/api.js` maps one-to-one onto the endpoints in `app/api/runs.py`. There
is no UI-only endpoint and no server-rendered view. That is what makes behaviour
4 structural rather than claimed: the browser cannot commit anything a script
could not, and cannot skip the gate. `test_the_surfaces_share_one_implementation`
already fails if a surface grows a decision of its own, and the UI was built to
stay inside that.

The consequence people ask about: **an approval clicked in the browser is
recorded as `decided_via = 'http'`, not `'ui'`.** The server cannot tell a
browser from `curl` — both are an HTTP POST with a JSON body. Writing `'ui'`
would be the unfalsifiable claim the column exists to prevent. `003_decided_via`
lists `'ui'` as a possible value; it is not one, and the comment is wrong.

### Design rules, and what they ruled out

**Show the evidence next to the decision.** A reviewer asked to approve
`hourly_rate: 2 distinct values` is not reviewing anything. The proposal card
shows USD 120 quoted from `invoice_1043.txt` at [484–491] beside USD 135 from
`amendment_01.md` at [277–321], with the rationale and the line saying nothing
has been resolved.

**No "approve all".** A control whose only purpose is to clear thirteen items
without reading them would quietly undo behaviour 3. Commit stays disabled while
anything is pending and puts the server's own sentence in its tooltip.

**One review, one request.** Verdicts are collected locally and sent as a single
`POST /decide` with mixed approvals and rejections. Per-item requests would leave
a half-reviewed run behind on any failure.

**Never show a number the screen was not given.** A run opened from the runs list
shows its status and nothing else — no document count, no fact count — because
this screen was not told those. Showing `0` there would be a made-up number in
the one place a reviewer looks to see how much the system read.

### Five bugs, found by driving it rather than reading it

**1. Thirteen clicks recorded one decision.** `setVerdicts({...verdicts, [id]:
v})` reads a stale closure: two verdicts set before React re-renders both read
the same `verdicts`, and the second overwrites the first. A reviewer clicking
quickly down the list would submit one decision believing they had submitted
thirteen — and the gate would still be holding twelve items with nothing saying
so. Fixed with the functional updater everywhere state derives from state.

Only visible because the page was driven. Reading the code, it looks right.

**2. A pile with seven read documents reported "0 docs, 7 not read".**
`list_piles` counted a document as read only if `status = 'ingested'`, and called
everything else a gap. But `classified` and `extracted` are states a document
reaches *by being understood*. So pipeline progress was counted as damage, and
the register composed from those documents' facts sat there beside the claim
that none of them had been read. Nothing failed; the number was simply a lie, in
the place a reviewer looks first.

Fixed by naming the statuses that actually mean not-read — `GAP_STATUSES =
('quarantined', 'unsupported')` — and by answering the question once, on the
server, as `is_gap` on each document row. The UI had the identical bug in its own
copy of the test, which is the argument for one answer rather than three.

**3. A browser refresh stranded the review.** The only handle on a run stopped at
the gate was a variable in the tab. Reload, and thirteen proposals sat in the
database with nothing able to name the run holding them. Behaviour 2 says a
stopped run can be picked back up; a UI that lost the run id on reload was not
honouring that. Added `list_runs(pile_id, status=None)` — an operation, so it is
on HTTP *and* MCP, and the parity test made that mandatory rather than optional.
An agent that lost its run id now has the same way back that a person does.

**4. The MCP stdio test was a race, and the earlier fix hid it.** It wrote three
messages with `subprocess.run(input=...)`, which closes stdin immediately — so it
raced the server's own shutdown-on-EOF. It won on an idle machine and lost under
a full suite. The previous fix raised the timeout to 180s, which treated a race
as slowness and left it flaky. Now the pipe is held open until the reply arrives,
as a real client would, with a watchdog so a genuinely hung server still fails
instead of hanging the suite. Reproduced outside pytest first, so the fix is
aimed at the cause.

**5. On a fresh clone, the demo pile produced an empty register and called it a
success.** The worst one, and it had been there the whole time — invisible
because every test starts from an empty pile and the UI was the first thing to
start from the seeded one.

`docker compose up` seeds `acme` by ingesting its bytes. `ingest` then skipped
all seven documents as duplicates, queued none of them, extracted nothing, and
composed six sections with **0 citations and 15 gaps** — while reporting "7
documents" and opening the gate as though it had worked. So the very first thing
a reviewer saw on a fresh clone was an empty register behind a successful run.

The cause is one confused sentence: *already stored* was treated as *already
understood*. They are different claims, and the content hash only ever
established the first. Fixed by keying the queue on whether anything has read
the document (`status = 'ingested'` means nothing has) rather than on whether
its bytes were new to this run — which also keeps a re-sent document a no-op,
because a classified or quarantined document has been decided about.

The register was at least honest about being empty; the run was not honest about
why. Two tests now pin both directions, and the first one fails against the old
code.

### The path that stays honest

Driven end to end in a browser, twice: once on a pile created through the UI, and
once on the seeded `acme` pile after `docker compose down -v` and a `--no-cache`
image build, which is the fresh-clone path behaviour 6 has to keep working now
that a node stage is in it. Both reach the same six hashes.

Read the pile →
13 proposals → 12 approved and `hourly_rate` rejected in one request → commit →
version 1, six sections, hashes `f2094b8a d87f2b4f fdfdcea8 88ff8462 5695b9a8
fe71da09`, byte-identical to every earlier run of this corpus. Then the
amendment arrives → 7 proposals, `billing` untouched → commit → version 2, four
written and two carried at their old hashes. Then the same bytes again → 0 model
calls, `no_change`, and all six sections reported unchanged *because they were
recomposed and hashed*, which the screen says in those words.

---

## The second corpus, and the hole it found

`corpora/pile_northwind/` — nine documents, Northwind Logistics Group and
Harbourline Freight Systems, drayage rather than fabrication. Not a rename of
acme: different sector, different document structure, different formats, and
above all a different answer.

### The claim it exists to support

A system tuned until one corpus comes out right is not a system that works — it
is a corpus that has been fitted to. So northwind was designed so that **the
rules acme breaks are the rules northwind satisfies, and the reverse, exactly**:

| | acme | northwind |
|---|---|---|
| conflicts | 3 | 1 |
| violated | `hours_within_sow_cap`, `rate_matches_current_agreement`, `payment_terms_consistent`, `termination_notice_observed` | `invoice_within_term`, `liability_cap_present`, `no_instructions_in_sources` |
| satisfied | the three northwind breaks | the four acme breaks |
| unjudgeable | `amendment_references_parent` | `amendment_references_parent` |
| quarantined | 0 | 1 |
| unsupported | 0 | 1 |
| formats exercised | md, txt, html | md, txt, html, **docx**, csv (refused) |

`test_the_answers_are_not_the_same_answers` asserts the two sets are exact
complements. A playbook whose findings were an artefact of the code rather than
of the documents could not produce that.

### The case acme does not contain

Northwind's MSA says USD 95 and its amendment says USD 110, so the documents
disagree and that reaches the gate as a conflict. But every invoice bills at the
amended rate, so `rate_matches_current_agreement` is **satisfied**. A conflict is
a question about what the documents say; a finding is a judgement about whether
anyone is failing to comply. On acme the two fire together, which is exactly the
configuration in which collapsing them would never be noticed.

### What it found: the register was silent about documents it never read

The deliverable has a section called *What Could Not Be Established*. Until this
corpus existed it only ever listed missing **fields** — because acme contains no
quarantined and no unsupported document, so the section was never asked the
question. Northwind has both, and both were simply absent: a reviewer reading the
register had no way to learn that two of the nine documents in the pile had never
been read at all.

Gaps are output, not error handling, and a gap that is silently omitted is worse
than a missing field, because nothing hints that anything is missing. `_gaps` now
reports whole documents alongside missing fields, for quarantined, unsupported
and escalated alike:

```
| (whole document) | rate_card_2026.csv | the format is not supported, so the document was never read |
| (whole document) | vendor_letter.md   | quarantined for containing instructions aimed at the system |
| liability_cap    | —                  | not stated in any document in the pile                      |
```

Acme's six section hashes are unchanged by this, because acme has no such
documents — checked, not assumed.

### Two bugs in the recorder, which the handoff tells people to run

`scripts/record_fixtures.py` is the one script a reviewer might actually execute,
and it could not record this corpus.

**It crashed on a document it was supposed to skip.** An unsupported format
raised out of `detect_format` and killed the run, so a corpus containing a
deliberate gap was impossible to record. Ingest refuses that file before any
stage asks a model about it; the recorder now says so and continues.

**It recorded outages as answers.** The free model returns `502
ResourceExhausted` under load, and the retry only matched `429`. Worse,
`classify_document` catches provider errors and turns them into an escalation
rather than raising — correct in a run, wrong here — so two documents were
written into the corpus as "escalated, confidence 0.00" and the recording
finished looking successful with two fixtures missing. It now retries the whole
capacity family and inspects the returned note, not only the exception.

Both fired on the real re-record: two retries, nine documents handled, 46 facts,
**$0.00**.

### Standing cost

14 model calls for 9 documents. The quarantined letter costs nothing because the
deterministic screen fires before the model call — there is no reason to spend a
call interpreting a document already known to be manipulating the interpreter,
and doing so would give the manipulation one chance to work. The refused csv
costs nothing because it never reaches a stage.

---

## PICK UP HERE

### State as of 2026-08-07

252 tests green offline, $0.00 spent. (The commit count used to live here. It
was wrong three times, always for the same reason — written before the commit
that carried it — so it is `git rev-list --count HEAD` now and not a number kept
by hand. A fact that has to be manually synchronised with a fact the tool
already knows will drift; that is worth a line here because the same reasoning
is why the register recomputes hashes instead of tracking what changed.)
**Behaviours 1–10 all done**, **all three movements exist** — understand,
examine, stay alive — **three working surfaces** over one operations layer
(HTTP, MCP, and a review UI at `/review/`), and **two corpora that produce
different answers**. Verified against `docker compose up` from a cold,
volume-less start, not only in the suite.

**Task 1 is complete.** Every graded behaviour, all three movements, three
surfaces, two corpora, and the documentation to read it by.

### Remaining

**Tasks 2, 3 and 4**, none of which is code in this repo:

1. **Task 2 — the SuperDocs build.** Product Manual and Quick-Start Builder,
   shipping as a PR into `superdocsapp/superdocs-builds` at
   `use-cases/CulturalProfessor/`. The four-call spine is verified working; see
   PLAN.md §4 for the three graded criteria and the mechanism answering each.
2. **Task 3 — the use-case list.**
3. **Task 4 — the demo video and write-up.** PLAN.md §5 has the schedule; the
   material is in this file under "Worth putting in the write-up", and the
   README's *The calls I made* is most of the argument already.

### The two open gaps, now closed by building them

Both were "either do it or stop claiming it". Both were first answered by
stopping the claim in writing, which is what this section used to record — and
then, two days later, by doing the thing instead, which is the better half of
that choice. The retractions are described here rather than deleted, because
the order this happened in is part of the record.

- **The folder watcher exists.** `app/watch.py` polls `WATCH_DIR`, and a file
  landing there becomes an arrival like any other: the same operation `POST
  /arrivals` and `doctask_document_arrived` call, halted at the same gate, with
  no privileged path of its own. It waits for a file to stop growing before
  touching it, and stops retrying one it cannot read rather than retrying it
  forever; there are tests for both. The earlier note here said none was built
  and that `WATCH_DIR` was declared but unconsumed. That is no longer true, in
  PLAN.md or in this file. (`5c9f973`)
- **pgvector is not cut.** Cut-list item 2 was taken and then given back,
  because the retrieval it buys turned out not to be a chat box over the corpus
  but two specific jobs: entity resolution escalating a near-matching
  counterparty name to a person instead of silently splitting the pile, and
  `GET /piles/{id}/search` returning passages with the same provenance a
  register cell carries. The first closed a real hole — an abbreviated name that
  made the register look clean because it was only ever comparing a document
  with itself. The README's *Vector search, and what it is for* is the current
  account, including the part that stays honest about the embedder being hashed
  character n-grams rather than a neural model. (`d11de80`)

### Worth putting in the write-up

- LangGraph's default `durability="async"` is the ordering that loses work. The
  framework gives you a checkpoint, not behaviour 2.
- The claim about concurrent runs that had to be withdrawn after measuring it.
- Where configuration-over-code stops in the playbook, stated rather than
  claimed away.
- Why EXAMINE calls no model.
- `decided_via`: how behaviour 3 and behaviour 4 were reconciled instead of one
  being chosen over the other.

### Notes for whoever picks this up

- The graph is `app/graph/build.py`; read `checkpoint.py`, `locking.py` and
  `state.py` first, they carry the decisions everything else follows from.
- Nodes never call `commit()`. If a new node does, it has broken behaviour 2.
- A node may run twice. Anything it writes must be safe to write again, or it
  belongs before the interrupt rather than after it — which is why `gate` writes
  nothing and `propose` exists separately.
- State is plain JSON on purpose. If something needs a custom serialiser to
  cross a node boundary, load it from the database instead.
- New capabilities go in `app/operations.py`, never in a surface. Two tests will
  fail if a surface grows a decision of its own.
- Re-recording fixtures is only needed if a *prompt* changes:
  `LLM_PROVIDER=openrouter RECORD_FIXTURES=1 .venv/bin/python -m scripts.record_fixtures`
