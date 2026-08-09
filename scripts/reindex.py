"""Backfill embeddings for spans written before the vector index existed.

The normal path embeds a span in the same INSERT that creates it, so this is not
part of any run. It exists for two situations that are both real:

  * A database that already held facts when migration 005 was applied. Those
    spans have NULL embeddings and are invisible to search, and a search that
    silently answers from half the pile is worse than one that refuses.
  * A change to the embedder. Vectors made by two different functions are not
    comparable, so swapping `get_embedder()` means re-embedding everything, and
    the honest way to do that is a command rather than a hope.

    python -m scripts.reindex          # spans with no vector
    python -m scripts.reindex --all    # every span, after an embedder change
"""
from __future__ import annotations

import sys

from app.retrieval.embed import embed_or_none, get_embedder
from app.retrieval.search import vector_literal
from app.store.engine import execute, fetch_all, transaction

BATCH = 500


def reindex(rebuild_all: bool = False) -> tuple[int, int]:
    """Returns (embedded, skipped). Skipped spans carry no alphanumeric text and
    are genuinely not retrievable by similarity -- reported rather than hidden,
    because a caller comparing counts should be able to see the difference
    between "not indexed yet" and "not indexable"."""
    embedded = skipped = 0
    # Keyset pagination on the primary key, not OFFSET. A span whose text has no
    # features stays NULL after this pass, so an OFFSET-based loop over
    # `embedding IS NULL` would return the same unembeddable batch forever.
    cursor = "00000000-0000-0000-0000-000000000000"
    condition = "" if rebuild_all else "AND embedding IS NULL"
    with transaction() as conn:
        while True:
            rows = fetch_all(conn, f"""
                SELECT id, text FROM span
                WHERE id > %s {condition}
                ORDER BY id LIMIT %s
            """, (cursor, BATCH))
            if not rows:
                break
            for row in rows:
                cursor = str(row["id"])
                vector = embed_or_none(row["text"])
                if vector is None:
                    skipped += 1
                    continue
                execute(conn, "UPDATE span SET embedding = %s::vector WHERE id = %s",
                        (vector_literal(vector), row["id"]))
                embedded += 1
    return embedded, skipped


if __name__ == "__main__":
    rebuild = "--all" in sys.argv
    print(f"reindexing with {get_embedder().name}"
          f"{' (every span)' if rebuild else ' (spans with no vector)'}")
    done, no_features = reindex(rebuild)
    print(f"  {done} span(s) embedded, {no_features} skipped for having no "
          f"alphanumeric text")
    sys.exit(0)
