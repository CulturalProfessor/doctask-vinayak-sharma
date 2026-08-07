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
> incremental update — over HTTP, over MCP, and in a browser. Sections marked
> TODO below are documentation that is not written yet, not features being
> claimed early.

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

One full corpus ships today:

- `corpora/pile_acme/` — seven documents, three real conflicts, four rule
  violations.
- `corpora/pile_northwind/` — **empty.** A second corpus with different
  counterparties and a different conflict profile is planned and not written;
  seeding skips it rather than pretending. Until it exists, "it works on a
  second pile" is not a claim this repo gets to make.

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

245 tests, all green. Runs with no API key and no network — `LLM_PROVIDER=fake`
uses a deterministic recorded provider, and there is a test that fails if
anything in the suite reaches the network. The tests that matter target
behaviours, not mocks: a run killed with `SIGKILL` mid-extraction and resumed,
two runs racing for one pile, a document that tries to give orders, a re-ingest
of identical bytes that changes nothing, a clean corpus that honestly reports no
findings, and a full pile driven from empty to committed register over MCP with
no HTTP and no browser.

## Security note

A source document is data, never instruction. Text inside a document that
addresses the system is reported as a finding and never acted on. There is a
poisoned fixture in the suite that proves it.
