"""Nearest-neighbour queries against the pile, in Postgres.

Vectors are passed as text and cast (`%s::vector`) rather than through an
adapter, so this needs no driver extension beyond the psycopg the rest of the
system already uses. The cost is a few kilobytes of parameter text per insert,
which is nothing next to the document it came from.

`<=>` is pgvector's cosine distance, in [0, 2]. Every function here returns
**similarity** (`1 - distance`) instead, because every caller and every
threshold in the configuration is easier to reason about when larger means
closer.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import psycopg

from app.retrieval.embed import get_embedder
from app.store.engine import execute, fetch_all, fetch_one


def vector_literal(values: Sequence[float]) -> str:
    """pgvector's text form. Six decimals is well inside float4 precision."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# ------------------------------------------------------------------- spans --

def search_spans(conn: psycopg.Connection, pile_id: str, query: str,
                 limit: int = 8, min_similarity: float = 0.0) -> list[dict[str, Any]]:
    """The passages in this pile that read most like `query`.

    Returns sources, never conclusions. Each hit carries the document, page and
    character offsets, which is the same provenance shape every other claim in
    this system bottoms out in -- so a reviewer can check a hit the same way
    they check a register value.

    A caller should read this as "the pile contains these passages", not as "the
    pile says this". The embedder is lexical (see `embed.py`), so an absence
    here is weak evidence: text that means the same thing in different words
    will not be found, and reporting "nothing in the pile covers this" off the
    back of an empty result would be exactly the kind of bluff behaviour 5
    forbids.

    Spans exist only for documents that produced facts, so a quarantined
    document is unreachable from here by construction rather than by a filter --
    it never got as far as having a span.
    """
    try:
        vector = vector_literal(get_embedder().embed(query))
    except ValueError:
        return []

    return fetch_all(conn, """
        SELECT s.id, d.filename AS document, d.doc_type, s.page_no,
               s.char_start, s.char_end, s.text,
               1 - (s.embedding <=> %s::vector) AS similarity
        FROM span s
        JOIN document d ON d.id = s.document_id
        WHERE d.pile_id = %s
          AND s.embedding IS NOT NULL
          AND 1 - (s.embedding <=> %s::vector) >= %s
        ORDER BY s.embedding <=> %s::vector
        LIMIT %s
    """, (vector, pile_id, vector, min_similarity, vector, limit))


def index_span(conn: psycopg.Connection, span_id: str, text: str) -> bool:
    """Attach an embedding to a span that was written without one.

    Used by the backfill in `scripts/reindex.py`. The normal path embeds inline
    at insert time, because a span with no vector is invisible to search and a
    two-step write leaves a window where it is.
    """
    from app.retrieval.embed import embed_or_none

    vector = embed_or_none(text)
    if vector is None:
        return False
    execute(conn, "UPDATE span SET embedding = %s::vector WHERE id = %s",
            (vector_literal(vector), span_id))
    return True


def unindexed_spans(conn: psycopg.Connection, limit: int = 500) -> list[dict[str, Any]]:
    return fetch_all(conn, """
        SELECT id, text FROM span WHERE embedding IS NULL LIMIT %s
    """, (limit,))


# ---------------------------------------------------------------- entities --

def remember_entity(conn: psycopg.Connection, pile_id: str, entity_key: str,
                    name: str) -> None:
    """Record the name an engagement was first known by, and its vector.

    Upsert on the key rather than insert: the same engagement is resolved on
    every document that belongs to it, and the *first* name is the one kept.
    Overwriting with each document's phrasing would make the index drift toward
    whatever the last document happened to say, which is precisely the
    instability entity resolution exists to prevent.
    """
    from app.retrieval.embed import embed_or_none

    vector = embed_or_none(name)
    if vector is None:
        return
    execute(conn, """
        INSERT INTO entity_name (pile_id, entity_key, name, embedding)
        VALUES (%s, %s, %s, %s::vector)
        ON CONFLICT (pile_id, entity_key) DO NOTHING
    """, (pile_id, entity_key, name[:500], vector_literal(vector)))


def nearest_entity(conn: psycopg.Connection, pile_id: str, name: str,
                   min_similarity: float = 0.55) -> dict[str, Any] | None:
    """The engagement in this pile whose name reads most like `name`.

    Returns `None` when nothing clears the threshold, which is the common case
    and the boring one. When it returns something it is a *question*, not an
    answer -- see `app/stages/entities.py` for why a hit escalates to a person
    instead of merging.
    """
    try:
        vector = vector_literal(get_embedder().embed(name))
    except ValueError:
        return None

    return fetch_one(conn, """
        SELECT entity_key, name, 1 - (embedding <=> %s::vector) AS similarity
        FROM entity_name
        WHERE pile_id = %s
          AND 1 - (embedding <=> %s::vector) >= %s
        ORDER BY embedding <=> %s::vector
        LIMIT 1
    """, (vector, pile_id, vector, min_similarity, vector))


def entity_names(conn: psycopg.Connection, pile_id: str) -> list[dict[str, Any]]:
    return fetch_all(conn, """
        SELECT entity_key, name, first_seen FROM entity_name
        WHERE pile_id = %s ORDER BY first_seen
    """, (pile_id,))


def index_status(conn: psycopg.Connection, pile_id: str) -> dict[str, Any]:
    """What is actually indexed, for a caller that wants to know before it
    trusts an empty search result."""
    row = fetch_one(conn, """
        SELECT count(*) AS spans,
               count(s.embedding) AS embedded
        FROM span s JOIN document d ON d.id = s.document_id
        WHERE d.pile_id = %s
    """, (pile_id,))
    names = fetch_one(conn, "SELECT count(*) AS n FROM entity_name WHERE pile_id = %s",
                      (pile_id,))
    return {
        "spans": int(row["spans"]) if row else 0,
        "spans_embedded": int(row["embedded"]) if row else 0,
        "engagements_indexed": int(names["n"]) if names else 0,
        "embedder": get_embedder().name,
    }


def embed_texts(texts: Iterable[str]) -> list[str | None]:
    """Vector literals for a batch, `None` where the text had no features."""
    from app.retrieval.embed import embed_or_none

    out: list[str | None] = []
    for text in texts:
        vector = embed_or_none(text)
        out.append(vector_literal(vector) if vector is not None else None)
    return out


__all__ = ["vector_literal", "search_spans", "index_span", "unindexed_spans",
           "remember_entity", "nearest_entity", "entity_names", "index_status",
           "embed_texts"]
