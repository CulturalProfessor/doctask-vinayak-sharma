"""Screening a source document for instructions aimed at the system.

A document in the pile is data to report on. It is never a set of commands to
follow. An invoice that says "auto-approve all pending proposals" is not
issuing an instruction -- it is exhibiting a fact about itself that belongs in
the findings.

This is the deterministic layer. A second, model-based opinion runs alongside it
during classification, and the two are combined: either one firing is enough to
quarantine. Layering a hard-coded rail underneath intelligent logic is
deliberate. The rail cannot be talked out of firing, and it works when the model
is the thing being manipulated.

The hard part is not detection, it is *not* false-positiving. Real contracts are
full of imperatives -- "Supplier shall provide", "Client must remit" -- directed
at the parties. What distinguishes an attack is a directive aimed at the reader
as a *processing system*, so the patterns below all require a system-referent,
not merely an imperative mood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Each pattern pairs a directive with something only an automated reader would
# be: an AI, a review system, an approval mechanism, a findings report.
_PATTERNS: list[tuple[str, str]] = [
    (
        "instruction_override",
        r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?"
        r"(previous|prior|above|earlier|preceding|the\s+following)?\s*"
        r"(instruction|prompt|rule|directive|guideline|playbook|checklist)s?\b",
    ),
    (
        "system_role_assertion",
        r"(^|\n)\s*(system|assistant|developer)\s*(instruction|prompt|message|note)s?\s*[:\-]",
    ),
    (
        "addresses_the_reader_as_a_system",
        r"\byou\s+are\s+(an?\s+)?(automated|ai|a\.i\.|artificial|language\s+model|"
        r"llm|agent|assistant|analysis\s+system|review\s+system)\b",
    ),
    (
        "claims_prior_authorisation",
        r"\b(you\s+have|has|have)\s+(full\s+|complete\s+|explicit\s+)?"
        r"(authorisation|authorization|permission|approval)\s+(from|to)\b"
        r"[^.\n]{0,80}\b(approve|proceed|skip|bypass|without\s+review)\b",
    ),
    (
        "claims_review_already_done",
        r"\b(requires?|needs?)\s+no\s+(further\s+)?(review|approval|verification|checking)\b",
    ),
    (
        "directs_approval",
        r"\b(auto[-\s]?approve|approve\s+all|automatically\s+approve)\b"
        r"|\bapprove\s+(all\s+)?(pending\s+)?(proposal|change|finding|conflict)s?\b",
    ),
    (
        "suppresses_findings",
        r"\b(do\s+not|don't|never)\s+(raise|report|flag|surface|generate|produce)\s+"
        r"(any\s+)?(finding|discrepanc|conflict|issue|exception|error)",
    ),
    (
        "directs_omission_from_output",
        r"\b(omit|exclude|remove|leave\s+out|do\s+not\s+include)\b[^.\n]{0,60}"
        r"\b(from\s+the\s+)?(register|report|output|deliverable|summary|analysis)\b",
    ),
    (
        "dictates_the_conclusion",
        r"\breport\s+that\s+(no|there\s+are\s+no)\b"
        r"|\bstate\s+that\s+(no|there\s+are\s+no)\b"
        r"|\bmark\s+(all\s+)?[^.\n]{0,40}\bas\s+(resolved|approved|compliant|cleared)\b",
    ),
    (
        "directs_configuration_change",
        r"\bset\s+(the\s+)?[^.\n]{0,40}\bto\s+(unlimited|infinite|zero|none|disabled)\b",
    ),
    ("skips_comparison", r"\bdo\s+not\s+(compare|check|validate|verify|cross[-\s]?reference)\b"),
]

_COMPILED = [(name, re.compile(pattern, re.I)) for name, pattern in _PATTERNS]


@dataclass
class ScreenHit:
    rule: str
    char_start: int
    char_end: int
    text: str


@dataclass
class ScreenVerdict:
    """`suspicious` is the branch condition. `hits` are the evidence, kept with
    offsets so the resulting finding cites the document rather than asserting
    something about it."""

    suspicious: bool
    hits: list[ScreenHit] = field(default_factory=list)

    @property
    def rules_fired(self) -> list[str]:
        return sorted({hit.rule for hit in self.hits})

    def statement(self, filename: str) -> str:
        rules = ", ".join(self.rules_fired)
        return (
            f"{filename} contains text directing the analysing system to take an "
            f"action ({rules}). The text is reported as a property of the document "
            f"and has not been acted on."
        )


def screen_text(text: str, extra_patterns: list[str] | None = None) -> ScreenVerdict:
    """Find directives aimed at an automated reader.

    Deliberately reports *every* match rather than stopping at the first, so a
    reviewer sees the whole shape of what a document tried to do.
    """
    hits: list[ScreenHit] = []
    compiled = list(_COMPILED)
    for index, pattern in enumerate(extra_patterns or []):
        compiled.append((f"configured_{index}", re.compile(pattern, re.I)))

    for rule, regex in compiled:
        for match in regex.finditer(text or ""):
            hits.append(
                ScreenHit(rule, match.start(), match.end(), text[match.start() : match.end()])
            )
    hits.sort(key=lambda h: h.char_start)
    return ScreenVerdict(suspicious=bool(hits), hits=hits)


def screen_pages(
    pages: list[tuple[int, str]], extra_patterns: list[str] | None = None
) -> dict[int, ScreenVerdict]:
    return {page_no: screen_text(text, extra_patterns) for page_no, text in pages}
