-- doctask initial schema.
--
-- The load-bearing idea: provenance is the row shape, not a feature. A fact
-- cannot exist without a span, and a span cannot exist without a document. See
-- PLAN.md section 2.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- the pile --

CREATE TABLE pile (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL UNIQUE,
    domain        TEXT NOT NULL,               -- selects config/domains/<domain>/
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id           UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    uri               TEXT NOT NULL,
    filename          TEXT NOT NULL,
    content_sha256    TEXT NOT NULL,
    byte_size         BIGINT NOT NULL,
    format            TEXT NOT NULL,           -- txt | md | html | pdf | docx
    doc_type          TEXT,                    -- from config taxonomy; NULL until classified
    doc_type_conf     REAL,
    -- ingested: bytes stored. classified/extracted: pipeline progress.
    -- quarantined: contains instructions aimed at the system (behaviour 8).
    -- unsupported: format we do not accept; recorded as an explicit gap.
    status            TEXT NOT NULL DEFAULT 'ingested',
    ingest_note       TEXT,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Re-ingesting identical bytes into the same pile is a no-op, not a
    -- duplicate. This constraint is the whole idempotency story.
    UNIQUE (pile_id, content_sha256)
);

CREATE INDEX document_pile_status_idx ON document (pile_id, status);

-- The extracted text of a document, one row per page (pdf) or per logical
-- block (docx). Text formats produce a single page 1. char offsets in `span`
-- are relative to the `text` of the page they cite.
CREATE TABLE page (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no       INT NOT NULL,
    text          TEXT NOT NULL,
    UNIQUE (document_id, page_no)
);

-- The provenance atom. Every citation in the system bottoms out here.
CREATE TABLE span (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no       INT NOT NULL,
    char_start    INT NOT NULL,
    char_end      INT NOT NULL,
    text          TEXT NOT NULL,
    embedding     vector(1024),
    CHECK (char_end > char_start)
);

CREATE INDEX span_document_idx ON span (document_id, page_no);

-- ------------------------------------------------------------------- runs --

CREATE TABLE run (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id               UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL,       -- full | incremental
    status                TEXT NOT NULL DEFAULT 'running',
    trigger_document_id   UUID REFERENCES document(id) ON DELETE SET NULL,
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at              TIMESTAMPTZ
);

-- One row per stage entered. `path_taken` is what makes behaviour 1 auditable:
-- it records which branch the stage chose and why, not merely that it ran.
CREATE TABLE stage_event (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,
    decision      TEXT,
    path_taken    TEXT,
    attempt       INT NOT NULL DEFAULT 1,
    document_id   UUID REFERENCES document(id) ON DELETE SET NULL,
    ms            INT,
    tokens_in     INT NOT NULL DEFAULT 0,
    tokens_out    INT NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    model         TEXT,
    detail        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX stage_event_run_idx ON stage_event (run_id, created_at);

-- ------------------------------------------------------------------ facts --

CREATE TABLE fact (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id         UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    -- NOT NULL is the point. There is no way to record a fact without saying
    -- where it came from.
    span_id         UUID NOT NULL REFERENCES span(id) ON DELETE CASCADE,
    run_id          UUID REFERENCES run(id) ON DELETE SET NULL,
    entity_key      TEXT NOT NULL,             -- e.g. "engagement:northwind-msa-2026"
    field           TEXT NOT NULL,             -- e.g. "hourly_rate"
    value_raw       TEXT NOT NULL,
    value_norm      TEXT,
    value_type      TEXT NOT NULL,             -- money | duration | date | text | number
    unit            TEXT,
    effective_from  DATE,
    confidence      REAL NOT NULL DEFAULT 1.0,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX fact_group_idx ON fact (pile_id, entity_key, field);

-- -------------------------------------------------------------- conflicts --

CREATE TABLE conflict (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id               UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    entity_key            TEXT NOT NULL,
    field                 TEXT NOT NULL,
    -- Precedence proposes; it never resolves. This column is a suggestion that
    -- still has to clear the gate. See PLAN.md section 2.
    proposed_fact_id      UUID REFERENCES fact(id) ON DELETE SET NULL,
    proposed_rationale    TEXT,
    status                TEXT NOT NULL DEFAULT 'open',   -- open | approved | rejected
    detected_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pile_id, entity_key, field)
);

CREATE TABLE conflict_member (
    conflict_id   UUID NOT NULL REFERENCES conflict(id) ON DELETE CASCADE,
    fact_id       UUID NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
    PRIMARY KEY (conflict_id, fact_id)
);

-- --------------------------------------------------------------- findings --

CREATE TABLE finding (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id       UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    run_id        UUID REFERENCES run(id) ON DELETE SET NULL,
    rule_key      TEXT NOT NULL,
    severity      TEXT NOT NULL,               -- info | low | medium | high
    statement     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE finding_citation (
    finding_id    UUID NOT NULL REFERENCES finding(id) ON DELETE CASCADE,
    span_id       UUID NOT NULL REFERENCES span(id) ON DELETE CASCADE,
    fact_id       UUID REFERENCES fact(id) ON DELETE SET NULL,
    PRIMARY KEY (finding_id, span_id)
);

-- ------------------------------------------------------------ deliverable --

CREATE TABLE deliverable (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id       UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    version       INT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pile_id, version)
);

-- content_hash is what turns "this update touched nothing else" from a promise
-- into an assertion. Behaviour: an update should cost like an update.
CREATE TABLE section (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deliverable_id    UUID NOT NULL REFERENCES deliverable(id) ON DELETE CASCADE,
    section_key       TEXT NOT NULL,
    ordinal           INT NOT NULL,
    body              TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    UNIQUE (deliverable_id, section_key)
);

CREATE TABLE section_citation (
    section_id    UUID NOT NULL REFERENCES section(id) ON DELETE CASCADE,
    fact_id       UUID NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
    PRIMARY KEY (section_id, fact_id)
);

-- --------------------------------------------------------------- the gate --

-- Nothing reaches the deliverable without a row here reaching 'approved'.
-- Decisions are per-item: rejecting one proposal leaves its siblings alone.
CREATE TABLE proposal (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id       UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    run_id        UUID NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,               -- conflict | finding | section_patch | escalation
    ref_id        UUID,                        -- the conflict/finding/section it concerns
    summary       TEXT NOT NULL,
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    reason        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX proposal_run_status_idx ON proposal (run_id, status);

-- The commit record, and the answer to "what changed, when, and because of
-- which source". Not generated from logs -- it *is* the log.
CREATE TABLE audit (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id             UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    section_id          UUID REFERENCES section(id) ON DELETE SET NULL,
    section_key         TEXT NOT NULL,
    from_hash           TEXT,
    to_hash             TEXT,
    run_id              UUID REFERENCES run(id) ON DELETE SET NULL,
    cause_document_id   UUID REFERENCES document(id) ON DELETE SET NULL,
    proposal_id         UUID REFERENCES proposal(id) ON DELETE SET NULL,
    committed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_pile_idx ON audit (pile_id, committed_at DESC);
