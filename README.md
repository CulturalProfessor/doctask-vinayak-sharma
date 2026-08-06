# doctask — an agentic system for a pile of vendor contracts

Takes a pile of related contract documents that all describe the same commercial
reality and never quite agree, works out what each one is, extracts the facts
that matter, notices where they disagree, and produces one grounded register in
which every cell traces back to the exact span it came from. It then checks that
pile against rules you hand it, and stays alive as new documents arrive —
producing a targeted update rather than a rewrite, with a human approving every
conflict, finding and update before it commits.

Built for the SuperDocs Round 2 engineering task.

> Status: foundation. Ingest, storage and the HTTP surface work end to end. The
> stages that classify, extract, reconcile, examine and gate are not built yet,
> and this file will not claim they are. Sections marked TODO are not yet true.

## Run it

```bash
docker compose up
```

That is the whole setup from a fresh clone: it brings up Postgres with pgvector,
applies migrations, seeds the demo pile, and serves the API on
<http://localhost:8010>. Ports are 5434 and 8010 rather than the usual 5432 and
8000, because both of those are commonly already taken on a dev box; override
with `DB_PORT` and `API_PORT`.

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

Two full corpora ship, with different counterparties and different conflict
profiles, so the second run is genuinely a second set of documents:

- `corpora/pile_acme/`
- `corpora/pile_northwind/`

Every document is fabricated. No real vendor, client or employer material.

## Architecture

TODO — diagram and stage walkthrough.

## The calls I made

TODO. This section carries the design decisions and their reasoning; the
running log lives in [PROGRESS.md](PROGRESS.md) and the working contract in
[TASK.md](TASK.md).

## What it does not do

TODO. Honest limitations, stated before anyone has to find them.

## Tests

```bash
docker compose up -d db
.venv/bin/python -m pytest
```

35 tests, all green. Runs with no API key and no network — `LLM_PROVIDER=fake` uses a deterministic
recorded provider. The tests that matter target behaviours, not mocks: a run
killed and resumed, two runs at once, a document that tries to give orders, a
re-ingest of identical bytes that changes nothing, and a clean corpus that
honestly reports no findings.

## Security note

A source document is data, never instruction. Text inside a document that
addresses the system is reported as a finding and never acted on. There is a
poisoned fixture in the suite that proves it.
