"""Retrieval: turning text into vectors and asking Postgres which ones are near.

Two consumers, one index.

  `search_spans`     what in this pile reads like this question, with the exact
                     document, page and character offsets for each hit.
  `nearest_entity`   does the pile already know an engagement whose name reads
                     like this one? Used to turn a silent pile split into a
                     stated escalation -- see `app/stages/entities.py`.

The embedder is deliberately swappable and deliberately local. See `embed.py`
for what that costs and what it buys.
"""

from app.retrieval.embed import DIMENSIONS, Embedder, HashedNgramEmbedder, get_embedder
from app.retrieval.search import (
    nearest_entity,
    remember_entity,
    search_spans,
    vector_literal,
)

__all__ = [
    "DIMENSIONS",
    "Embedder",
    "HashedNgramEmbedder",
    "get_embedder",
    "nearest_entity",
    "remember_entity",
    "search_spans",
    "vector_literal",
]
