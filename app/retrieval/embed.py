"""Text to vector, with no key and no network.

## Why this embedder and not a hosted one

Behaviour 7 says the suite runs without a live key, and the container has to
reach a working system from a fresh clone with one command. An embedding model
behind an API key fails both: the tests would need a key or a second layer of
recorded fixtures, and `docker compose up` would produce a system whose
retrieval silently does nothing until someone signs up for something.

So the embedder that ships is local, deterministic, and honest about what it
is: **hashed character n-grams**, not a semantic model. It projects a string
into a fixed-width vector by hashing its character trigrams and its whole-word
tokens into buckets with signed weights, then L2-normalising. Cosine distance
over these vectors measures *lexical* similarity -- shared substrings and shared
words. "Acme Fabrication Services LLC" and "Acme Fabrication Svcs" land close
together; "Harbourline Freight" lands far from both. It does not know that
"vendor" and "supplier" mean the same thing, and this module does not pretend
otherwise anywhere it is used.

## What that buys, and what it does not

What it buys is real: real vectors, a real HNSW index, real approximate nearest
neighbour search in Postgres, and one function to change to make it semantic.
`get_embedder()` is the single swap point -- an implementation of `Embedder`
with `DIMENSIONS` outputs drops in without a schema change, a query change, or a
caller change, because nothing above this file knows how a vector was made.

What it does not buy is synonymy. A hosted embedder would find "the supplier may
terminate on notice" from a query about cancellation rights; this one will not
unless the words overlap. Where that matters is stated at the call site rather
than left for a reader to discover.

No such hosted implementation ships here, and that is a decision rather than an
omission: an untested provider in the tree would be a capability that is present
and broken, which is worse than one that is absent and documented.

## Determinism

The hash is blake2b over the feature bytes, so the same string produces the same
vector on every machine, every process and every run. That matters more than it
sounds: `span.embedding` is written once at extraction time and queried later,
so a non-deterministic embedder would make a document's own text stop matching
itself across a restart.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Protocol, runtime_checkable

# Fixed by `span.embedding vector(1024)` in migration 001. A different width is
# a migration, not a setting -- pgvector columns are typed, and a mismatch is an
# insert error rather than a silently truncated vector.
DIMENSIONS = 1024

_NGRAM = 3
_NON_WORD = re.compile(r"[^a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn a string into `DIMENSIONS` floats."""

    name: str
    dimensions: int

    def embed(self, text: str) -> list[float]:
        """The vector for `text`.

        Raises `ValueError` when the text carries no features at all. Returning
        a zero vector instead would be worse than failing: cosine distance
        against zero is undefined, and pgvector answers NaN, which sorts as a
        perfectly good match.
        """
        ...


class HashedNgramEmbedder:
    """Character trigrams and whole words, hashed into signed buckets."""

    name = "hashed-ngram-v1"

    def __init__(self, dimensions: int = DIMENSIONS, ngram: int = _NGRAM) -> None:
        self.dimensions = dimensions
        self.ngram = ngram

    def embed(self, text: str) -> list[float]:
        features = list(_features(text, self.ngram))
        if not features:
            raise ValueError("nothing to embed: the text has no alphanumeric content")

        vector = [0.0] * self.dimensions
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            # Signed hashing. Collisions are inevitable at 1024 buckets, and an
            # unsigned scheme makes every one of them add, which inflates the
            # similarity of unrelated strings. A sign bit makes collisions
            # cancel on average instead of accumulating.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # Every feature cancelled against a collision. Vanishingly unlikely
            # and not something to paper over with a fake vector.
            raise ValueError(f"embedding of {text[:60]!r} cancelled to zero")
        return [v / norm for v in vector]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _features(text: str, ngram: int) -> Iterable[tuple[str, float]]:
    """The pieces of a string worth hashing, each with a weight.

    Two kinds, because they fail in opposite directions. Trigrams survive
    abbreviation and misspelling ("Svcs" still shares three trigrams with
    "Services") but they also match on accidental substring overlap. Whole words
    do not survive abbreviation at all, but when they do match they are strong
    evidence -- so they carry more weight.
    """
    folded = _NON_WORD.sub(" ", (text or "").casefold()).strip()
    if not folded:
        return

    for token in folded.split():
        yield f"w:{token}", 1.5

    # Padded so that the first and last characters of the string participate in
    # a full trigram; without it a one-character difference at either end is
    # under-counted.
    padded = f" {folded} "
    for i in range(len(padded) - ngram + 1):
        gram = padded[i:i + ngram]
        if gram.strip():
            yield f"g:{gram}", 1.0


_default: Embedder | None = None


def get_embedder() -> Embedder:
    """The process's embedder.

    One instance, because the vectors written into `span.embedding` and the
    vectors a query is compared against have to come from the same function or
    the comparison is meaningless.
    """
    global _default
    if _default is None:
        _default = HashedNgramEmbedder()
    return _default


def embed_or_none(text: str) -> list[float] | None:
    """The vector, or nothing, for text that may legitimately have no features.

    Span text comes from real documents and is occasionally a bare number or a
    row of punctuation. That is not an error worth failing an extraction over --
    the fact still stores, it simply is not retrievable by similarity, and a
    NULL embedding says exactly that.
    """
    try:
        return get_embedder().embed(text)
    except ValueError:
        return None


__all__ = ["DIMENSIONS", "Embedder", "HashedNgramEmbedder", "get_embedder",
           "embed_or_none"]
