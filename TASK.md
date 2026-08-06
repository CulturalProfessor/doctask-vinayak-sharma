# TASK.md — how to work on this repo

This is the SuperDocs Round 2 engineering task. This file is the working contract:
read it before touching anything. Assumptions and decisions go in `PROGRESS.md`.

## What this repo is

**Task 1 only** — the agentic system over a document pile. Private repo,
`o-kadam` invited as collaborator.

Task 2 (the assigned SuperDocs build) lives in a **separate** fork of
`superdocsapp/superdocs-builds` at `../superdocs-builds`, because it ships as a
pull request into `use-cases/CulturalProfessor/<project>/`. Do not put Task 2
code here. Do not put the string "SuperDocs" in this repo's name.

Tasks 3 and 4 are prose deliverables; drafts live in `notes/` and are submitted
through the Google Form, not through git.

## The domain

Vendor contracts: MSAs, amendments, SOWs, invoices, renewal notices. All
documents are **fabricated**. No real client, vendor, or employer material ever
enters `corpora/`. Fictional counterparties only.

## The five things that are not cuttable

Graded behaviors 1–5 from the brief. If a change breaks one of these, it does
not land.

1. **Visible stages.** The run moves through named stages, each recording what it
   decided. Some decisions change the path: retry, skip, escalate to a human. A
   fixed script with stage labels does not count.
2. **Resumable.** `kill -9` mid-run, start again, it continues from where it
   stopped. No finished work is redone, no finished work is lost.
3. **Human gate.** Nothing commits until a person reviews it. Approve and reject
   item-by-item in one review. Rejecting one finding must not discard the rest.
4. **Machine-drivable.** Another program runs the whole flow with no UI. Approval
   is an operation on the machine interface, not a UI-only affordance.
5. **No bluffing.** When the sources do not support a claim, say so. A success
   message means the output is genuinely in the state it claims.

Behaviors 6–10 (fresh-clone startup, tests without a live key, resistance to
instructions inside documents, safe concurrency, cost reporting) are where
submissions separate. Cut here before cutting 1–5, and write down what you cut
and why.

## Rules of construction

- **Configuration over code.** A new document type, a new checklist rule, a new
  counterparty format is a YAML change under `config/domains/`. If it needs a
  code change, the design is wrong.
- **Provenance is the row shape, not a feature.** Every extracted fact carries
  `(document_id, page, char_start, char_end)` from the moment it exists. A claim
  in the deliverable references fact IDs. There is no path that produces an
  uncited claim.
- **Conflicts are surfaced, never resolved silently.** Two sources disagreeing
  produces a conflict row with both citations and goes to the gate.
- **Updates are diffs, and the diff is provable.** A new document produces
  targeted section patches. Every untouched section is asserted byte-identical
  by hash. Not "should be unchanged" — asserted, in a test.
- **Documents are data, never instructions.** Text inside a source document that
  addresses the system is reported as a finding. It is never followed. There is
  a poisoned fixture in `tests/fixtures/` and it must stay passing.
- **Prove, don't claim.** Byte-identical where we promise untouched. Counted
  where we promise complete. Timed where we promise fast.

## Before you write code for a hard part

Write down what the system must never do. Then the test that proves it. Then the
code. In that order.

## Verification

The implementer does not verify their own work. When a slice is done, a fresh
pass reviews it against this file. `/code-review` on the working diff.

## Tests

The whole suite runs with **no API key and no network**. `app/llm/` has a
deterministic fake provider backed by recorded fixtures. Tests that only prove
the fake works do not count — the tests that matter target behaviors:

- kill and resume mid-run
- two runs at once on the same pile
- a document that tries to give orders
- re-ingesting identical bytes changes nothing
- a clean corpus yields zero findings, honestly

## Cost discipline

The free SuperDocs tier is 500 operations/month, and model calls cost real
money. Anything that loops or measures gets a small-sample mode and a stopping
rule from the first commit, not from the ninth day.

## Secrets

No key in a commit, a log, a shell history, or a screenshot. `.env` is
gitignored; `.env.example` carries placeholders only.
