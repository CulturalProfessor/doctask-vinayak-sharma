"""Deciding which engagement a document belongs to.

Facts only meet if their entity keys agree. Get this wrong and the pile silently
splits: two engagements where there is one, no group ever holds more than one
document, and no disagreement is ever found. The register looks clean and is
worthless.

This existed as a one-line slug of whatever the model called the counterparty
until a real run broke it. Amendment 1 produced "Acme Fabrication Services LLC";
amendment 2, a near-identical document, produced "Brightwell Manufacturing Inc.
and Acme Fabrication Services LLC" -- both defensible readings of "who is the
agreement with", and they slug to different keys.

So resolution does not trust the model for identity. It takes the model's answer
as a *candidate name* and resolves it against the engagements the pile already
knows about, using rules that do not vary run to run:

  1. A configured alias wins outright.
  2. An exact slug match to a known engagement.
  3. Token containment -- the candidate names a known engagement plus extra
     words, which is what "A and B" does to "B".
  4. Nothing matched: this is a new engagement.

When a candidate contains *two* known engagements it is genuinely ambiguous, and
that escalates to a person. Both silent outcomes are bad -- merging two
engagements corrupts the register, splitting one hides every conflict -- so
neither is chosen automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NON_WORD = re.compile(r"[^a-z0-9]+")

# Legal-form noise that varies between documents describing the same company.
DEFAULT_STOPWORDS = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "sas", "bv", "nv", "pty", "and", "the",
}


@dataclass
class EntityResolution:
    entity_key: str | None
    method: str                    # alias | exact | contains | new | ambiguous
    confident: bool = True
    candidates: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def escalate(self) -> bool:
        return not self.confident


def slugify(value: str) -> str:
    return _NON_WORD.sub("-", (value or "").strip().casefold()).strip("-") or "unknown"


def significant_tokens(value: str, stopwords: set[str] | None = None) -> set[str]:
    """The words that actually identify a company.

    Dropping legal forms is what lets "Acme Fabrication Services LLC" and
    "Acme Fabrication Services, Inc." be recognised as the same party -- and,
    more importantly here, lets a two-party string be seen to *contain* a
    one-party one.
    """
    stops = DEFAULT_STOPWORDS if stopwords is None else stopwords
    return {t for t in _NON_WORD.sub(" ", (value or "").casefold()).split()
            if t and t not in stops}


def resolve_entity(candidate: str, known_keys: list[str], template: str,
                   cfg: dict | None = None) -> EntityResolution:
    """Resolve a counterparty name to an engagement key."""
    cfg = cfg or {}
    stopwords = set(cfg.get("stopwords", DEFAULT_STOPWORDS))
    if not (candidate or "").strip():
        return EntityResolution(None, "new", confident=False,
                                note="no counterparty named in the document")

    def key_for(slug: str) -> str:
        return template.replace("{counterparty_slug}", slug)

    # 1. A configured alias is an explicit human decision and outranks inference.
    for canonical, spellings in (cfg.get("aliases") or {}).items():
        if any(slugify(s) == slugify(candidate) for s in spellings) \
                or slugify(canonical) == slugify(candidate):
            return EntityResolution(key_for(slugify(canonical)), "alias")

    candidate_key = key_for(slugify(candidate))
    if candidate_key in known_keys:
        return EntityResolution(candidate_key, "exact")

    # 3. Containment. "Brightwell ... and Acme Fabrication Services LLC" carries
    #    every significant token of "Acme Fabrication Services LLC".
    candidate_tokens = significant_tokens(candidate, stopwords)
    matches: list[str] = []
    for known in known_keys:
        known_name = known.split(":", 1)[-1]
        known_tokens = significant_tokens(known_name.replace("-", " "), stopwords)
        if known_tokens and known_tokens <= candidate_tokens:
            matches.append(known)

    if len(matches) == 1:
        return EntityResolution(
            matches[0], "contains",
            note=(f"{candidate!r} names the existing engagement plus additional "
                  f"parties; attached to {matches[0]}"),
        )
    if len(matches) > 1:
        # Picking one would corrupt the register; creating a third would hide
        # every conflict. Neither is ours to choose.
        return EntityResolution(
            None, "ambiguous", confident=False, candidates=matches,
            note=(f"{candidate!r} names {len(matches)} known engagements "
                  f"({', '.join(matches)}); a person must say which one this "
                  f"document belongs to"),
        )

    return EntityResolution(candidate_key, "new",
                            note=f"no existing engagement matches {candidate!r}")
