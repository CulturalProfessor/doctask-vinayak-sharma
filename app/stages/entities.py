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
  4. Similarity -- nothing lexical matched, but the pile already holds an
     engagement whose name *reads* like this one.
  5. Nothing matched at all: this is a new engagement.

When a candidate contains *two* known engagements it is genuinely ambiguous, and
that escalates to a person. Both silent outcomes are bad -- merging two
engagements corrupts the register, splitting one hides every conflict -- so
neither is chosen automatically.

## Why step 4 exists

Steps 1 to 3 are exact. Containment requires *every* significant token of a
known name to appear in the candidate, so a single word that changed shape
defeats it: "Acme Fabrication Svcs" shares no token with
`acme-fabrication-services-llc` beyond the first two, containment fails, and the
old code fell straight through to step 5 and declared a second engagement. That
is the outcome this module's own docstring calls the worse one, and it happened
*silently* -- the register looked fine, and every disagreement between the two
halves of the split pile simply stopped being found.

Step 4 closes that. The candidate is compared against the names the pile's
engagements were first known by, using vector similarity in Postgres
(`app/retrieval/`). A hit does **not** merge. It returns `confident=False` and
sends the document to a person with the near match named, because a similarity
score is evidence that a question exists, never an answer to it. The failure
mode it removes is a silent split; the failure mode it must not introduce is a
silent merge, and an escalation is the only outcome that avoids both.

Resolution stays deterministic despite the new step: the embedder is a pure
function of the string (see `app/retrieval/embed.py`), so the same pile in the
same state gives the same answer on every run.

`near_match` is injected rather than imported so this module stays a pure
function of its arguments and keeps its tests free of a database.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

_NON_WORD = re.compile(r"[^a-z0-9]+")

# Legal-form noise that varies between documents describing the same company.
DEFAULT_STOPWORDS = {
    "inc",
    "incorporated",
    "llc",
    "llp",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "gmbh",
    "sa",
    "sas",
    "bv",
    "nv",
    "pty",
    "and",
    "the",
}


@dataclass
class EntityResolution:
    entity_key: str | None
    method: str  # alias | exact | contains | near | new | ambiguous
    confident: bool = True
    candidates: list[str] = field(default_factory=list)
    note: str | None = None
    similarity: float | None = None

    @property
    def escalate(self) -> bool:
        return not self.confident


class NearMatch(Protocol):
    """Ask the pile whether it already knows a name like this one.

    Returns a mapping with `entity_key`, `name` and `similarity`, or `None` when
    nothing is close enough. Implemented by
    `app.retrieval.search.nearest_entity`; supplied as a callable so this stage
    never touches a connection.
    """

    def __call__(self, name: str) -> dict | None: ...


# How close two names have to read before a person is asked about them.
#
# 0.55 is chosen against the failure it exists to catch rather than tuned for a
# score: abbreviations and legal-form changes of the same party land well above
# it, and unrelated counterparties in the same domain land well below. It is
# configurable per domain (`reconciliation.yaml`, `entity.near_match_similarity`)
# because the right value depends on how alike the names in a pile legitimately
# are -- a pile of subsidiaries of one group needs a higher bar than a pile of
# unrelated vendors.
#
# The cost of the threshold being too low is an escalation a person dismisses.
# The cost of it being too high is a silently split pile. They are not
# symmetrical, and this errs toward the first.
DEFAULT_NEAR_MATCH_SIMILARITY = 0.55


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
    return {t for t in _NON_WORD.sub(" ", (value or "").casefold()).split() if t and t not in stops}


def resolve_entity(
    candidate: str,
    known_keys: list[str],
    template: str,
    cfg: dict | None = None,
    near_match: NearMatch | Callable[[str], dict | None] | None = None,
) -> EntityResolution:
    """Resolve a counterparty name to an engagement key."""
    cfg = cfg or {}
    stopwords = set(cfg.get("stopwords", DEFAULT_STOPWORDS))
    if not (candidate or "").strip():
        return EntityResolution(
            None, "new", confident=False, note="no counterparty named in the document"
        )

    def key_for(slug: str) -> str:
        return template.replace("{counterparty_slug}", slug)

    # 1. A configured alias is an explicit human decision and outranks inference.
    for canonical, spellings in (cfg.get("aliases") or {}).items():
        if any(slugify(s) == slugify(candidate) for s in spellings) or slugify(
            canonical
        ) == slugify(candidate):
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
            matches[0],
            "contains",
            note=(
                f"{candidate!r} names the existing engagement plus additional "
                f"parties; attached to {matches[0]}"
            ),
        )
    if len(matches) > 1:
        # Picking one would corrupt the register; creating a third would hide
        # every conflict. Neither is ours to choose.
        return EntityResolution(
            None,
            "ambiguous",
            confident=False,
            candidates=matches,
            note=(
                f"{candidate!r} names {len(matches)} known engagements "
                f"({', '.join(matches)}); a person must say which one this "
                f"document belongs to"
            ),
        )

    # 4. Nothing lexical matched. Before declaring a second engagement -- the
    #    outcome that hides every disagreement between the two halves it creates
    #    -- ask whether the pile already knows a name that reads like this one.
    #
    #    A hit is escalated, never merged. The system knows enough to say "these
    #    two names may be the same party"; it does not know enough to say they
    #    are, and acting as though it did would corrupt the register in the one
    #    way that is hardest to notice afterwards.
    if near_match is not None:
        hit = near_match(candidate)
        if hit and hit.get("entity_key") and hit["entity_key"] != candidate_key:
            similarity = float(hit.get("similarity") or 0.0)
            return EntityResolution(
                None,
                "near",
                confident=False,
                candidates=[hit["entity_key"]],
                similarity=round(similarity, 4),
                note=(
                    f"{candidate!r} does not match any known engagement by "
                    f"name, but reads like {hit.get('name') or hit['entity_key']!r} "
                    f"({hit['entity_key']}, similarity {similarity:.2f}). "
                    f"Treating it as a new engagement would split the pile and "
                    f"hide every disagreement between the two; merging them is "
                    f"not something a similarity score can decide. A person "
                    f"must say whether this is the same party."
                ),
            )

    # 5. Genuinely new, by every test available.
    return EntityResolution(
        candidate_key, "new", note=f"no existing engagement matches {candidate!r}"
    )
