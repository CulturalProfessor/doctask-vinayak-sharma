-- Vector search, made load-bearing.
--
-- Migration 001 enabled the `vector` extension and gave `span` an `embedding`
-- column that nothing wrote and nothing read. This migration is where that
-- column starts earning its place: an index that makes it queryable, and a
-- second table so the same machinery can answer the one question that was
-- quietly going unasked -- "does this pile already know an engagement whose
-- name reads like this one?"
--
-- See app/retrieval/embed.py for what makes the vectors, and
-- app/stages/entities.py for what the entity index is used to prevent.

-- HNSW rather than IVFFlat: IVFFlat needs a populated table to build its lists
-- and degrades when the table grows past the distribution it was built on,
-- which is the wrong shape for a pile that never stops growing. HNSW builds on
-- an empty table and stays correct as rows arrive, which is what an index on a
-- watched corpus has to do.
--
-- Rows with a NULL embedding are simply not in the index. That is the right
-- behaviour and not a gap: a span whose text carries no alphanumeric features
-- is not retrievable by similarity, and a NULL says so honestly.
CREATE INDEX IF NOT EXISTS span_embedding_idx
    ON span USING hnsw (embedding vector_cosine_ops);

-- The name an engagement was first known by.
--
-- Not derived from `fact.entity_key`, because a key is a slug and a slug has
-- already thrown away the spelling that similarity needs: by the time
-- "Acme Fabrication Svcs" has become `engagement:acme-fabrication-svcs`, the
-- distance between it and `engagement:acme-fabrication-services-llc` is a
-- string-edit question, not a semantic one. The original text is kept so the
-- comparison happens on what the document actually said.
--
-- One row per engagement per pile. The FIRST name wins (see
-- app/retrieval/search.py:remember_entity) so the index does not drift toward
-- whatever the most recent document happened to call the counterparty.
CREATE TABLE IF NOT EXISTS entity_name (
    pile_id       UUID NOT NULL REFERENCES pile(id) ON DELETE CASCADE,
    entity_key    TEXT NOT NULL,
    name          TEXT NOT NULL,
    embedding     vector(1024) NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pile_id, entity_key)
);

CREATE INDEX IF NOT EXISTS entity_name_embedding_idx
    ON entity_name USING hnsw (embedding vector_cosine_ops);
