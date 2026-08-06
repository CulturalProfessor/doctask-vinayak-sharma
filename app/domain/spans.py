"""Locating a quoted passage in a document, exactly.

A model hands back the text it based a fact on. To turn that into provenance we
have to find where it actually sits in the source, as character offsets, in the
*original* text -- not in some cleaned-up copy, because the offsets have to
point at real bytes a reviewer can be shown.

Three things make that harder than `str.index`:

  Documents wrap. "USD 120 per hour" is "USD 120 per\\nhour" in the MSA. Matching
  has to be whitespace-tolerant while still reporting original offsets.

  Models paraphrase whitespace, casing and punctuation even when instructed not
  to, so an exact match cannot be the only strategy.

  Sometimes the quote is not in the document at all -- because the model
  produced it from memory or invented it. That case must return *nothing*. A
  span matcher that always finds something somewhere is a fabrication engine
  with extra steps, and every citation it produces would be worthless.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Below this similarity a candidate is not the quote, it is a different passage
# that happens to share words. Chosen to accept whitespace and punctuation drift
# while rejecting a quote that came from somewhere else entirely.
DEFAULT_MIN_SCORE = 0.86

_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class SpanMatch:
    char_start: int
    char_end: int
    text: str          # the original text at these offsets, verbatim
    method: str        # exact | whitespace | case_insensitive | fuzzy
    score: float


def _normalise(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs, keeping a map back to original offsets.

    `index_map[k]` is the offset in `text` of normalised character `k`, which is
    what lets a match found in normalised space be reported in original space.
    """
    out: list[str] = []
    index_map: list[int] = []
    at_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if not at_space:
                out.append(" ")
                index_map.append(i)
                at_space = True
        else:
            out.append(ch)
            index_map.append(i)
            at_space = False
    while out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


def _to_original(index_map: list[int], text_len: int, start: int, end: int) -> tuple[int, int]:
    char_start = index_map[start]
    char_end = index_map[end - 1] + 1 if end - 1 < len(index_map) else text_len
    return char_start, char_end


def find_span(haystack: str, needle: str,
              min_score: float = DEFAULT_MIN_SCORE) -> SpanMatch | None:
    """Locate `needle` in `haystack`, returning original offsets, or None.

    Returning None is a real answer. The caller records a gap rather than a
    citation, which is how the system avoids claiming a source it cannot show.
    """
    needle = (needle or "").strip()
    if not needle or not haystack:
        return None

    # 1. Exact. The cheap common case.
    at = haystack.find(needle)
    if at != -1:
        return SpanMatch(at, at + len(needle), haystack[at:at + len(needle)], "exact", 1.0)

    hay_norm, index_map = _normalise(haystack)
    needle_norm, _ = _normalise(needle)
    if not needle_norm:
        return None

    # 2. Whitespace-insensitive. This is the line-wrap case, and in a corpus of
    # real documents it is the case that actually fires.
    at = hay_norm.find(needle_norm)
    if at != -1:
        start, end = _to_original(index_map, len(haystack), at, at + len(needle_norm))
        return SpanMatch(start, end, haystack[start:end], "whitespace", 1.0)

    # 3. Case-insensitive, still whitespace-insensitive.
    at = hay_norm.lower().find(needle_norm.lower())
    if at != -1:
        start, end = _to_original(index_map, len(haystack), at, at + len(needle_norm))
        return SpanMatch(start, end, haystack[start:end], "case_insensitive", 0.99)

    # 4. Fuzzy, and only where the quote plausibly begins.
    best = _best_fuzzy(hay_norm, needle_norm, min_score)
    if best is None:
        return None
    at, length, score = best
    start, end = _to_original(index_map, len(haystack), at, at + length)
    return SpanMatch(start, end, haystack[start:end], "fuzzy", round(score, 4))


def _best_fuzzy(hay_norm: str, needle_norm: str,
                min_score: float) -> tuple[int, int, float] | None:
    """Score windows anchored where the quote's first word occurs.

    Anchoring keeps this linear in the number of occurrences of one word rather
    than quadratic in document length, and a quote that shares no word with the
    document is correctly unfindable.
    """
    words = _WORD.findall(needle_norm)
    if not words:
        return None
    anchor = words[0].lower()
    hay_lower = hay_norm.lower()

    starts: list[int] = []
    at = hay_lower.find(anchor)
    while at != -1 and len(starts) < 400:
        starts.append(at)
        at = hay_lower.find(anchor, at + 1)
    if not starts:
        return None

    target = len(needle_norm)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle_norm)

    best: tuple[int, int, float] | None = None
    # Try a few window lengths: a paraphrase is rarely the same length.
    for start in starts:
        for factor in (0.85, 1.0, 1.15):
            length = max(1, int(target * factor))
            window = hay_norm[start:start + length]
            if not window:
                continue
            matcher.set_seq1(window)
            # Cheap upper bound first; full ratio only where it could win.
            if matcher.real_quick_ratio() < min_score or matcher.quick_ratio() < min_score:
                continue
            score = matcher.ratio()
            if score >= min_score and (best is None or score > best[2]):
                best = (start, len(window), score)
    return best


def find_all_spans(haystack: str, needles: list[str],
                   min_score: float = DEFAULT_MIN_SCORE) -> dict[str, SpanMatch | None]:
    """Locate several quotes. Unfound quotes map to None, deliberately kept in
    the result so the caller can count and report them as gaps."""
    return {needle: find_span(haystack, needle, min_score) for needle in needles}
