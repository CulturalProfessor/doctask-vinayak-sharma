# doctask — an agentic system for a pile of vendor contracts

Takes a pile of related contract documents that all describe the same commercial
reality and never quite agree, works out what each one is, extracts the facts
that matter, notices where they disagree, and produces one grounded register in
which every cell traces back to the exact span it came from. It then checks that
pile against rules you hand it, and stays alive as new documents arrive —
producing a targeted update rather than a rewrite, with a human approving every
conflict, finding and update before it commits.

Built for the SuperDocs Round 2 engineering task.

> Status: all three movements work end to end — understand, the human gate, the
> incremental update — over HTTP, over MCP, and in a browser, on two corpora
> that produce different answers. [What it does not do](#what-it-does-not-do) is
> not a placeholder; read it.

## Run it

```bash
docker compose up
```

That is the whole setup from a fresh clone: it brings up Postgres with pgvector,
applies migrations, builds the review UI, seeds the demo pile, and serves
everything on <http://localhost:8010>. Ports are 5434 and 8010 rather than the
usual 5432 and 8000, because both of those are commonly already taken on a dev
box; override with `DB_PORT` and `API_PORT`.

Three ways in, over one set of operations:

| | |
|---|---|
| **Review UI** | <http://localhost:8010/review/> |
| **HTTP API** | <http://localhost:8010/docs> |
| **MCP** | `python -m app.mcp.server` (stdio) |

None of them is privileged. The UI cannot commit anything a script could not,
and cannot skip the gate — every one of them calls the same functions in
`app/operations.py`, and there are tests that fail if a surface grows a decision
of its own.

```bash
curl localhost:8010/health
curl localhost:8010/piles
```

Seeding is idempotent — it is content-addressed, so running it again reports
every document as already present and changes nothing.

## What it accepts

**Domain:** vendor contracting. Master services agreements, amendments,
statements of work, invoices, renewal and termination notices.

**Formats:** `txt`, `md`, `html`, `pdf`, `docx`.

A file in any other format is not dropped — it is recorded with status
`unsupported` and a note saying why, because a pile that quietly lost a document
produces a register that is confidently wrong.

**A limitation, stated up front:** character offsets are exact for `txt`, `md`,
`html` and `docx`. For `pdf`, offsets are exact *within an extracted page* and
citations are honest at page granularity — PDF text extraction does not give
reliable offsets back into the original layout, and pretending otherwise would
make every PDF citation quietly wrong.

Two corpora ship, and they are deliberately not variations on each other:

- `corpora/pile_acme/` — seven documents. Three conflicts, four rule violations.
- `corpora/pile_northwind/` — nine documents, a different counterparty in a
  different sector. One conflict, three violations, one document that tries to
  give the system orders, and one in a format the system refuses.

The rules that fire on acme are the rules that pass on northwind, and the
reverse, exactly — there is a test asserting it. That inversion is the point of
having a second corpus: a playbook whose findings were an artefact of the code
rather than of the documents would produce the same shape on both.

Every document is fabricated. No real vendor, client or employer material.

## Architecture

One LangGraph `StateGraph`, checkpointed into Postgres, with a real branch at
every point where the system might be wrong.

```
  corpus / arrival
         │
         ▼
   ┌───────────┐
   │  INGEST   │  content hash, format detect, dedupe
   └─────┬─────┘
         ▼
   ┌───────────────────┐
   │  SELECT DOCUMENT  │◀───────────────────────────────────────────────┐
   └─────┬─────────────┘                                                │
         │                                                              │
         ├─── queue empty ────▶ RECONCILE, below                        │
         ▼                                                              │
   ┌───────────┐                                                        │
   │ CLASSIFY  │─── quarantined ───▶ never extracted; becomes a finding ─┤
   └─────┬─────┘                                                        │
         ├─── confidence < 0.70 ──▶ escalated; not extracted ───────────┤
         ▼                                                              │
   ┌───────────┐                                                        │
   │  EXTRACT  │─── facts + spans + gaps ──────────────────────────────┘
   └───────────┘

   ┌───────────┐   more than 25 open    ┌──────────────────┐
   │ RECONCILE │───────────────────────▶│ ESCALATE VOLUME  │
   └─────┬─────┘                        └────────┬─────────┘
         │◀──────────────────────────────────────┘
         ▼
   ┌───────────┐
   │  COMPOSE  │  the register; every cell cited or an explicit gap
   └─────┬─────┘
         ▼
   ┌───────────┐
   │  EXAMINE  │  the playbook — violated / satisfied / not enough evidence
   └─────┬─────┘
         ▼
   ┌───────────┐
   │   DELTA   │  what moved, measured by recomputed hash
   └─────┬─────┘
         ▼
   ┌───────────┐   nothing to review
   │  PROPOSE  │──────────────────────▶ END
   └─────┬─────┘
         ▼
   ┌───────────┐
   │   GATE    │  interrupt() — a person decides, and the run stops here
   └─────┬─────┘
         ▼
   ┌───────────┐
   │  COMMIT   │  writes exactly what was approved, and nothing else
   └───────────┘
```

**INGEST** hashes the bytes. Re-ingesting identical bytes into the same pile is
a no-op enforced by a unique constraint, not by a check that can be forgotten. A
document already stored but never read is still queued — *already stored* and
*already understood* are different claims.

**CLASSIFY** screens for injected instructions before it calls a model, then
picks a document type. Below 0.70 confidence it escalates rather than guessing,
because everything downstream extracts against the chosen type's schema and a
confident wrong classification poisons every fact after it.

**EXTRACT** pulls the fields that type's schema asks for, and locates each one's
quote in the source text — exact, then whitespace-tolerant, then
case-insensitive, then fuzzy above a similarity floor. `fact.span_id` is `NOT
NULL`: a fact that cannot be traced to a span cannot be stored. Fields the
document does not answer become gaps, which are output.

**RECONCILE** groups facts by `(entity, field)` and calls any group with more
than one distinct normalised value a conflict. Type precedence produces a
*proposed* resolution — never an applied one. Above 25 open conflicts the run
escalates as a whole instead of flooding the gate.

**COMPOSE** renders the register from reconciled facts. A cell with no citation
is rendered as an explicit gap, never as a blank.

**EXAMINE** checks the pile against `rules/playbook.yaml` and answers each rule
*violated*, *satisfied*, or *not enough evidence*. It calls no model: these are
arithmetic and date comparisons, and a finding a reviewer has to trust is worth
more than one a reviewer has to check.

**DELTA** recomputes every section's hash and compares it to the committed
version. "Unchanged" is therefore evidence, not an assumption about what a new
document could have touched.

**GATE** calls `interrupt()` and writes nothing — a node that interrupts
re-executes from the top when the run resumes, so anything it wrote would be
written twice. **COMMIT** writes only approved items, refusing any section whose
hash no longer matches the one on the proposal.

### Storage

Postgres, 17 tables. The shape carries the invariants:

| | |
|---|---|
| `pile` → `document` → `page` → `span` | text, and the offsets everything cites |
| `fact` | `span_id NOT NULL` — provenance is the row shape |
| `conflict` / `conflict_member` | disagreements, and every document in each |
| `finding` / `finding_citation` | what the playbook said, with its evidence |
| `deliverable` → `section` | the register, versioned |
| `proposal` | what was put to a person, and what they said |
| `audit` | from-hash, to-hash, cause document, when |
| `run` / `stage_event` / `model_call` | the trail, the cost, and the ledger |
| `checkpoints` (LangGraph) | resumability |

`langgraph` 1.2.10 · `langgraph-checkpoint-postgres` 3.1.1 · `mcp` 2.0.0 ·
`fastapi` 0.141.1 · `psycopg` 3.3.4.

## The calls I made

The running log with the measurements is in [PROGRESS.md](PROGRESS.md); the
working contract is [TASK.md](TASK.md). These are the decisions that shaped
everything else.

**Provenance is the row shape, not a convention.** `fact.span_id` is `NOT NULL`.
There is no code path that can store an uncited fact, because the database
refuses it. Putting the invariant in the schema rather than in a review comment
is the difference between a rule and a hope.

**Conflicts are surfaced and never resolved.** Type precedence produces a
proposal with its reasoning; nothing applies it. A rejected conflict does not
disappear — it stays visible as unresolved, which is the honest state. The
system's job is to make disagreement impossible to miss, not to pick a winner.

**A conflict and a finding are different things.** A conflict is a question about
what the documents *say*; a finding is a judgement about whether anyone is
failing to comply. `pile_northwind` has a rate conflict whose rule is satisfied —
the MSA and the amendment disagree, and every invoice follows the amendment. On
`pile_acme` the two always fire together, which is exactly the corpus where
collapsing them would never be noticed.

**Three outcomes, not two.** *Not enough evidence* is not *satisfied*. A rule the
pile could not answer is not a rule the pile passed, and reporting eight rules
checked and six held is only worth trusting if the other two are named.

**`durability="sync"`.** LangGraph's default writes checkpoints on a background
thread while the next node runs. On a shared connection that collides; more
importantly, checkpoint and work are two writes and *either* ordering loses
silently. This was the central lesson of the port: the framework gives you a
checkpoint, not behaviour 2.

**One operations layer, three surfaces.** HTTP, MCP and the review UI are
argument parsing over `app/operations.py` and contain no decisions of their own.
One test fails if a surface reaches past it into the machinery; another fails if
an operation exists that the machine interface cannot reach. The repo learned this the
expensive way — a full run and an incremental update were once two code paths,
and an entity-resolution fix landed on one and not the other.

**`decided_via` is set by the surface, not the caller.** Behaviour 3 says a
person holds the gate; behaviour 4 says a machine must be able to drive approval
end to end. Both hold only if the record can tell them apart afterwards. A
decision made in the browser records `http`, not `ui` — the server cannot
distinguish a browser from `curl`, and claiming otherwise would be exactly the
unfalsifiable thing that column exists to prevent.

**A document is data, never instruction.** A deterministic screen runs *before*
the model call and short-circuits it, with a model opinion layered alongside.
Either firing is enough. The rail cannot be talked out of firing, and it works
when the model is the thing being manipulated.

**Configuration-over-code, with the boundary stated.** A rule shaped like an
existing one is a YAML change and nothing else — there is a test that adds one
and gets a cited finding without touching Python. A rule needing a new kind of
arithmetic is a new check kind, and that is Python. Six kinds cover the eight
rules that ship. Claiming the stronger version would survive exactly until the
first reviewer tried it.

**Recorded fixtures, not mocks.** `LLM_PROVIDER=fake` replays what a real model
actually said to the same prompt. A missing recording fails loudly with the
filename to create rather than inventing a plausible answer, because a suite that
can invent answers proves nothing.

## What it does not do

Stated here rather than left to be discovered.

**There is no folder watcher.** `WATCH_DIR` is set in `docker-compose.yml` and
nothing consumes it. A document arriving is an *operation* — `POST /arrivals`,
`doctask_document_arrived`, or the button in the UI — and something outside this
system has to call it. The incremental movement is real and tested; the trigger
for it is a call, not a filesystem event.

**pgvector is installed and unused.** The extension is enabled and `span` has an
`embedding` column that nothing populates or queries. On a corpus of this size,
exact and fuzzy span matching does the retrieval job and semantic search would
add latency for no measured gain — so this is cut-list item 2, taken. The column
stays because removing it would be pretending the option was never considered;
it is not evidence of a feature.

**No authentication, on any surface.** Every endpoint and every MCP tool is
open. `decided_by` is whatever the caller says it is. The audit trail records a
claim about who decided, not a verified identity, and it should not be read as
one.

**One domain ships.** `config/domains/vendor_contracts/` is the only
configuration. The loading is generic and a second domain is YAML, but nobody
has written one, so "domain-agnostic" is a design property here rather than a
demonstrated one.

**PDF citations are page-granular.** Offsets are exact within an extracted page.
PDF text extraction does not give reliable offsets back into the original layout.

**Cross-currency values are never converted.** A group mixing USD and EUR is
raised as a conflict rather than compared, because comparing them needs a rate
and a date, and guessing produces a confidently wrong answer.

**`amendment_references_parent` cannot be judged.** No extraction schema declares
`amends_agreement`, so nothing in the pile can answer that rule. It reports
*not enough evidence* and says why. That is a gap in this repo's configuration,
not in the documents — calling it a violation would blame the sources for our
omission. Adding the field would change a prompt and require re-recording.

**Cost figures come from the provider.** `stage_event` sums what the provider
reported. The recorded fixtures were made against a zero-priced model, so every
number in the demo's cost table is `$0.0000` — the plumbing is real, the amounts
are not evidence of anything.

**English only, and no localisation.** Cut-list item 4 was never reached.

## Tests

```bash
docker compose up -d db
.venv/bin/python -m pytest
```

252 tests, all green. Runs with no API key and no network — `LLM_PROVIDER=fake`
replays recorded responses, and tests pin that default and LangSmith's tracing
flags so neither can drift into reaching the network on someone else's machine.
(Those pin the configuration; nothing blocks sockets at runtime, so this is a
guarantee about defaults, not an enforced sandbox.) The tests that matter target
behaviours, not mocks: a run killed with `SIGKILL` mid-extraction and resumed,
two runs racing for one pile, a document that tries to give orders, a re-ingest
of identical bytes that changes nothing, a clean corpus that honestly reports no
findings, and a full pile driven from empty to committed register over MCP with
no HTTP and no browser.

## Security note

A source document is data, never instruction. Text inside a document that
addresses the system is reported as a finding and never acted on. There is a
poisoned fixture in the suite that proves it.
