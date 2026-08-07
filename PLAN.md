# PLAN.md — what we are building and how

Deadline **20 August 2026**. Target done **18 August**, leaving two days of
buffer. Working contract is [TASK.md](TASK.md); running decisions are in
[PROGRESS.md](PROGRESS.md).

---

## 1. The thing itself

**doctask** owns a pile of vendor contracts end to end.

A pile is an MSA, its amendments, the SOWs underneath it, the invoices billed
against it, and the renewal and termination notices around it. They all describe
the same commercial relationship and they do not agree. Amendment 2 raised the
rate and nobody told billing. The SOW promises a delivery date the MSA's notice
period makes impossible. The invoice bills 40 hours against a cap of 35.

The system does three things with that pile.

**It understands it.** Mixed formats in, one grounded deliverable out: an
*obligations and commercials register*, one row per term, every cell tracing to
the exact span of the exact document it came from.

**It examines it.** You hand it a contract playbook — a YAML checklist of rules
you care about. It checks the sources and the register against those rules in
stages and produces findings, each pointing at where it came from. A clean pile
produces an honest report of nothing found.

> **Status, 7 Aug: true.** `app/stages/examine.py`. Rules are arithmetic over
> the extracted facts rather than model judgements, because a finding a reviewer
> can check beats one they have to trust — and it costs nothing on a stage that
> runs on every arrival. Each rule has **three** outcomes: `violated`,
> `satisfied`, and `not_enough_evidence`. The third is what keeps "nothing found"
> honest, because a rule nobody could check is not a rule that passed.

**It stays alive.** New documents land in a watched folder. Each arrival produces
a *targeted update* to the register, not a rewrite and not a re-run that happens
to reproduce the same bytes. Sections the new document did not affect stay
byte-identical, and the system proves it. Where the new document contradicts what
the register already says, the conflict is surfaced, never silently resolved.

A human approves every conflict, every finding and every update before it commits.

### Why vendor contracts

The disagreements are numeric and provable. "The MSA says USD 120/hr, amendment 2
says USD 135/hr effective 1 June, invoice 1043 billed 120 for July work" is a
conflict a viewer understands in four seconds with no domain vocabulary. That
matters for a three-minute demo video. Everything is fabricated.

---

## 2. Architecture

```
                    ┌──────────────┐
   watched folder → │   INGEST     │ hash, dedupe, format detect
   REST upload    → └──────┬───────┘
                           ↓
                    ┌──────────────┐  low confidence ──→ escalate to human
                    │  CLASSIFY    │
                    └──────┬───────┘
                           ↓
                    ┌──────────────┐  validation fail ──→ retry ──→ skip + log gap
                    │  EXTRACT     │  facts, each bound to a source span
                    └──────┬───────┘
                           │  instructions detected in document
                           ├──────────────────────→ QUARANTINE (becomes a finding)
                           ↓
                    ┌──────────────┐
                    │  NORMALIZE   │ currency, dates, units, entity keys
                    └──────┬───────┘
                           ↓
                    ┌──────────────┐
                    │  RECONCILE   │ group by (entity, field) → conflicts
                    └──────┬───────┘
                           ↓
     ┌──────────────┬──────┴───────┐
     ↓              ↓              ↓
┌─────────┐   ┌──────────┐   ┌──────────┐
│ COMPOSE │ → │ EXAMINE  │ → │  DELTA   │
│ register│   │ playbook │   │ which    │
│ sections│   │→ findings│   │ sections │
└────┬────┘   └────┬─────┘   └────┬─────┘
     └─────────────┴──────────────┘
                   ↓
            ┌─────────────┐
            │    GATE     │ ← LangGraph interrupt. Halts. Nothing commits.
            └──────┬──────┘   Per-item approve / reject.
                   ↓
            ┌─────────────┐
            │   COMMIT    │ → audit: what changed, when, because of which source
            └─────────────┘
```

Orchestration is **LangGraph with a Postgres checkpointer**, so resumability is a
property of the graph rather than bespoke state-machine code I have to defend in
the write-up.

> **Status, 7 Aug: true.** `app/graph/build.py` is the graph above; `interrupt()`
> is the gate. One graph serves both movements — a full run is a queue of seven
> documents against an empty pile, an update is a queue of one against a
> committed register — so there is no second pipeline to keep in agreement.
>
> The load-bearing decision was not the framework, it was how the checkpoint
> relates to the work. They are two writes, and either ordering loses silently:
> work committed without the checkpoint means a resumed run redoes a node;
> checkpoint committed without the work means it skips one. So there is one
> write — the checkpointer shares the run's connection and commits it when it
> persists the step. LangGraph's default durability is `async`, which is the
> losing ordering, so runs are driven with `durability="sync"`.
>
> Model calls are protected separately, because the node that was in flight when
> a process dies genuinely does re-run. Every completion is recorded under
> `(run_id, call_key)` and committed outside the run's transaction, so a resumed
> run replays its answers rather than buying them twice. See
> `app/graph/checkpoint.py` and `app/llm/durable.py`.

### The decisions that change the path

The brief is explicit that a fixed script with stage labels is not an agentic
system. Four decisions genuinely redirect control flow:

| Decision | Branch |
|---|---|
| Classification confidence below threshold | escalate to a human proposal instead of guessing a type |
| Extraction fails schema validation | retry under a stricter prompt, then skip and log an explicit gap |
| Document contains instructions aimed at the system | quarantine — it becomes a finding, never an input |
| Conflict volume above threshold | escalate the whole run rather than flooding the gate |

### Data model

The load-bearing idea: **provenance is the row shape, not a feature.**

```
pile            (id, name, domain_config)
document        (id, pile_id, uri, content_sha256, format, doc_type, status)
                UNIQUE (pile_id, content_sha256)      ← idempotent re-ingest
span            (id, document_id, page_no, char_start, char_end, text)
fact            (id, pile_id, document_id, span_id, entity_key, field,
                 value_raw, value_norm, unit, effective_from, confidence, run_id)
conflict        (id, pile_id, entity_key, field, status, proposed_resolution)
conflict_member (conflict_id, fact_id)
finding         (id, pile_id, rule_key, severity, statement, status, run_id)
finding_citation(finding_id, fact_id)
deliverable     (id, pile_id, version)
section         (id, deliverable_id, section_key, ordinal, body, content_hash)
section_citation(section_id, fact_id)
proposal        (id, run_id, kind, payload, status, decided_at, reason)
run             (id, pile_id, kind, status, trigger_document_id)
stage_event     (id, run_id, stage, decision, path_taken, attempt,
                 ms, tokens_in, tokens_out, cost_usd, model)
audit           (id, section_id, from_hash, to_hash, run_id,
                 cause_document_id, proposal_id, committed_at)
```

Three consequences fall out of this shape rather than being bolted on:

- A fact cannot exist without a `span_id`. There is no code path that produces an
  uncited claim, because the type system will not let one be constructed.
- `section.content_hash` is what makes "this update touched nothing else" an
  assertion instead of a promise.
- `audit` *is* the answer to "what changed, when, and because of which source."
  It is not a report generated from logs; it is the commit record.

`pgvector` holds span embeddings, used for retrieval during EXAMINE — finding the
passages a rule needs to be judged against.

### One subtlety worth naming

Amendments supersede the MSA. It is tempting to encode that precedence and
auto-resolve. The brief says conflicts must be surfaced, not silently resolved.

The resolution: precedence produces a **proposed** resolution attached to the
conflict, ranked and explained. It still goes to the gate. It is never applied
on its own. The system gets to be useful without getting to be unilateral.

---

## 3. How each graded behavior is met

Behaviors 1–5 are the floor and are not cuttable. 6–10 are where submissions
separate.

| # | Requirement | Mechanism | Proof |
|---|---|---|---|
| 1 | Visible stages, some decisions change the path | `stage_event` per node + SSE to the UI; four real branches above | Run log shows branch taken and why |
| 2 | Survives being stopped | LangGraph Postgres checkpoint committed in the same transaction as the node's work; model answers recorded per run | Test: two real `SIGKILL`s mid-run — 14 answers bought across both processes, register byte-identical to an uninterrupted run |
| 3 | A human holds the gate | `proposal` rows + graph `interrupt()`; per-item decisions | Test: reject one finding of three, other two survive |
| 4 | A machine can drive it | MCP server + REST as thin surfaces over one `app/operations.py`; `decide` is a tool | Test: a pile driven from empty to committed register through MCP alone, mixed approve/reject in one review; a test asserts neither surface reaches past `operations` |
| 5 | It never bluffs | Claims require ≥1 citation; composer refuses uncited output; a playbook rule has three outcomes, so "could not be checked" is never reported as "passed" | Test: clean corpus → honest zero findings, and a pile with no facts says no rule could be judged rather than no violations |
| 6 | A stranger can run it | `docker compose up`, seeded | Fresh-clone rehearsal on day 11 |
| 7 | Real tests, no live key | `LLM_PROVIDER=fake`, deterministic recorded provider | Whole suite green offline |
| 8 | Takes no orders from documents | Quarantine branch | Poisoned fixture in the suite |
| 9 | Two runs stay two runs | Session-scoped advisory lock per pile, held for a run's working phase and released across the gate | Test: two real processes contending — without the lock a reviewer gets 15 items where there are 9 |
| 10 | Knows what it cost | `stage_event` tokens/cost/ms → `GET /runs/{id}/report` | Report printed in the demo |

### Configuration over code

A new document type, rule, counterparty format or register column is a YAML
change. If it needs Python, the design is wrong.

```
config/domains/vendor_contracts/
  doc_types.yaml          taxonomy + classification hints
  extraction/*.yaml       per-type field schemas
  normalization.yaml      currency, date, unit handling
  reconciliation.yaml     entity keys, precedence, numeric tolerance
  rules/playbook.yaml     the checklist, each rule naming a check kind
  register.yaml           deliverable shape
```

---

## 4. Task 2 — the assigned SuperDocs build

Ships separately as a PR into `superdocsapp/superdocs-builds` at
`use-cases/CulturalProfessor/`. **Not in this repo.**

Product Manual and Quick-Start Builder. One structured product spec — product,
parts graph, setup steps, warnings, specifications — and the manual and the
quick-start card are two *projections* of it. They cannot disagree because
neither is authored; both are generated.

Graded on three things, and the API has a primitive for each. The four-call
spine is already verified working (see PROGRESS.md, 7 Aug):

| Criterion | Mechanism |
|---|---|
| Quick-start stays consistent when the manual changes | Both documents open in one **multi-document session**; a single edit turn returns `proposed_change_batch` grouped by `document_id`. One instruction produced both edits, so drift is structurally impossible |
| Diagrams regenerate rather than being re-drawn | Exploded-parts and per-step diagrams render **deterministically as SVG from the parts graph**; on edit, re-render → `upload_image_base64` → swap one `<img src>` in one chunk. No model ever draws |
| Localized versions keep page structure | Skeleton saved via `upload_template_base64`; per-locale content filled into the fixed skeleton; section counts and page-break positions **asserted** equal to source locale |

---

## 5. Schedule

Thirteen days. Evenings on weekdays, two full weekends (9–10 and 16–17 August).

| Dates | Work |
|---|---|
| **Aug 7–8** (Thu–Fri) | Foundation: compose, schema, migrations, config loader, fake LLM provider, ingest with hashing and dedupe |
| **Aug 9–10** (weekend) | UNDERSTAND end to end: classify → extract with spans → normalize → compose. First grounded register |
| **Aug 11–12** | RECONCILE + conflicts. EXAMINE + rules + findings |
| **Aug 13–14** | The GATE: proposals, interrupt, per-item approve/reject, commit + audit |
| **Aug 15** | Task 2: parts graph → deterministic SVG → manual + quick-start in one multi-doc session |
| **Aug 16–17** (weekend) | Watcher + incremental update + byte-identity proof. MCP server. React review UI |
| **Aug 18** | Behavior tests (kill/resume, concurrency, injection, idempotency, clean corpus), cost report, fresh-clone rehearsal, second corpus |
| **Aug 19** | Task 2 localization + README + PR. Task 3 use-case list |
| **Aug 20** | Task 4: demo video, one-page write-up, architecture diagram. Submit |

That last day is thin, and the video is the thing everything else is read
through. If Aug 16–17 slips, **Task 4 does not move** — the cut comes out of
section 6 below.

### Declared cut list, in order

The brief rewards a defended cut over a hollow stage. Cuts happen in this order,
and each one goes in the write-up with its reasoning:

1. **React UI degrades to a minimal review table.** The gate must exist and be
   usable; it does not have to be pretty. Behavior 3 survives; polish does not.
2. **Vector retrieval degrades to lexical + structured lookup.** If pgvector is
   not earning its latency on a corpus this size, saying so is more honest than
   keeping it for the stack checkbox.
3. **Second corpus shrinks** from a full pile to a smaller one — still genuinely
   different documents, so "it works the second time" still holds.
4. **Localization narrows to two locales** rather than several. The pagination
   proof is the point, not the language count.

Behaviors 1–5 are never cut. If they are at risk, scope comes off the register's
column count, not off the floor.

---

## 6. What I expect to be hard

Named now so the write-up can be honest later rather than retrofitted.

- **Span-accurate provenance through PDF extraction.** Character offsets survive
  plain text cleanly; PDFs with tables less so. Likely outcome is page-plus-bbox
  granularity for PDFs and exact char offsets for text formats, declared as such
  rather than papered over.
- **"Targeted update, not a rewrite" under a model that likes rewriting.** The
  defence is structural: the composer only receives the affected sections, so a
  rewrite of anything else is not possible rather than merely discouraged.
- **Honest zero findings.** Models want to be helpful and will manufacture a
  finding. Needs an explicit no-findings path and a test that a clean corpus
  produces silence.
- **Concurrency on a graph framework.** LangGraph checkpointing is per-thread;
  two runs on one pile contend at the section level, not the graph level. The
  lock has to live in my schema, not theirs.
