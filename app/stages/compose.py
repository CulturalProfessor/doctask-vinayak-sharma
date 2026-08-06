"""Building the register.

Two properties this stage owes the rest of the system.

**Every cell is cited or is explicitly a gap.** There is no third state. A blank
cell would imply "nothing to say here" when the truth is "we could not
establish this", and those are different claims. `register.yaml` sets
`uncited_cell_behaviour: render_as_gap`, and this honours it.

**Sections are the unit of change, and each carries a content hash.** An
incremental update rewrites only the sections a new document affects; every
other section keeps its hash, which is how "this update touched nothing else"
becomes something the system proves rather than promises. That makes
determinism load-bearing: the same facts must render the same bytes, so
everything here is ordered explicitly rather than relying on dict order.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field as dc_field
from typing import Iterable

from app.domain.config import DomainConfig
from app.domain.models import SourcedFact
from app.stages.extract import Gap
from app.stages.reconcile import Conflict

NOT_ESTABLISHED = "_not established_"


@dataclass
class Section:
    key: str
    heading: str
    ordinal: int
    body: str
    content_hash: str
    citations: list[str] = dc_field(default_factory=list)
    gap_count: int = 0


@dataclass
class Register:
    title: str
    sections: list[Section]

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    @property
    def hashes(self) -> dict[str, str]:
        """The fingerprint an incremental update is checked against."""
        return {s.key: s.content_hash for s in self.sections}

    def render(self) -> str:
        parts = [f"# {self.title}", ""]
        for section in self.sections:
            parts.append(f"## {section.heading}")
            parts.append("")
            parts.append(section.body)
            parts.append("")
        return "\n".join(parts)


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def compose(cfg: DomainConfig, facts: Iterable[SourcedFact], conflicts: list[Conflict],
            gaps: list[tuple[str, Gap]]) -> Register:
    """Render the register from reconciled facts.

    `gaps` are (document, Gap) pairs carried through from extraction, so the
    register can report what could not be established alongside what could.
    """
    by_field: dict[str, list[SourcedFact]] = {}
    for sourced in facts:
        by_field.setdefault(sourced.field, []).append(sourced)
    for members in by_field.values():
        members.sort(key=lambda m: (m.document, m.fact.span.char_start))

    conflicted_fields = {c.field for c in conflicts}
    sections: list[Section] = []

    for ordinal, spec in enumerate(cfg.register.get("sections", [])):
        source = spec.get("source")
        if source == "conflicts":
            body, citations, gap_count = _render_conflicts(conflicts)
        elif source == "gaps":
            body, citations, gap_count = _render_gaps(gaps, cfg, by_field)
        else:
            body, citations, gap_count = _render_fields(
                spec.get("fields", []), by_field, conflicted_fields
            )
        sections.append(Section(
            key=spec["key"], heading=spec.get("heading", spec["key"]), ordinal=ordinal,
            body=body, content_hash=content_hash(body),
            citations=citations, gap_count=gap_count,
        ))

    return Register(title=cfg.register.get("title", "Register"), sections=sections)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_Nothing to report._"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        return "|" + "|".join(f" {c.ljust(widths[i])} " for i, c in enumerate(cells)) + "|"

    return "\n".join([
        line(headers),
        "|" + "|".join("-" * (w + 2) for w in widths) + "|",
        *(line(row) for row in rows),
    ])


def _trim(value: str, limit: int = 72) -> str:
    """Keep a cell readable. The full text is always one click away in the
    cited span, so truncating the rendered value loses nothing recoverable."""
    flat = " ".join(str(value).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _quote(sourced: SourcedFact, limit: int = 60) -> str:
    text = " ".join(sourced.fact.span.text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f'"{text}"'


def _render_fields(fields: list[str], by_field: dict[str, list[SourcedFact]],
                   conflicted: set[str]) -> tuple[str, list[str], int]:
    rows: list[list[str]] = []
    citations: list[str] = []
    gap_count = 0

    for name in fields:
        members = by_field.get(name, [])
        if not members:
            # An explicit gap, not a blank. "We could not establish this" and
            # "there is nothing to say here" are different claims.
            rows.append([name, NOT_ESTABLISHED, "—", "—"])
            gap_count += 1
            continue
        for sourced in members:
            label = name + (" ⚠" if name in conflicted else "")
            span = sourced.fact.span
            rows.append([
                label, _trim(sourced.display), sourced.document,
                f"{_quote(sourced)} [{span.char_start}-{span.char_end}]",
            ])
            citations.append(sourced.citation)

    return _table(["Term", "Value", "Source", "Where it says so"], rows), citations, gap_count


def _render_conflicts(conflicts: list[Conflict]) -> tuple[str, list[str], int]:
    if not conflicts:
        # The rarest output in this industry, and it has to be sayable.
        return ("_No disagreements found. Every field the documents state is "
                "stated consistently._", [], 0)

    blocks: list[str] = []
    citations: list[str] = []
    for conflict in sorted(conflicts, key=lambda c: (c.entity_key, c.field)):
        rows = [[m.doc_type, m.document, _trim(m.display, 40),
                 f"{_quote(m, 44)} [{m.fact.span.char_start}-{m.fact.span.char_end}]"]
                for m in conflict.members]
        citations.extend(m.citation for m in conflict.members)
        blocks.append(f"### {conflict.field} — {len(conflict.distinct_values)} distinct values")
        blocks.append("")
        blocks.append(_table(["Type", "Document", "Value", "Where it says so"], rows))
        blocks.append("")
        if conflict.proposed is not None:
            blocks.append(f"**Proposed:** {_trim(conflict.proposed.display)} "
                          f"(from {conflict.proposed.document})")
        else:
            blocks.append("**Proposed:** none — the documents do not settle this.")
        blocks.append("")
        blocks.append(f"_Reasoning: {conflict.rationale}_")
        blocks.append("")
        # Said in the deliverable itself, not only in the code, because the
        # reader is the person who has to act on it.
        blocks.append("_Status: **open**. This is a proposal for review. "
                      "No value has been resolved or applied._")
        blocks.append("")
    return "\n".join(blocks).rstrip(), citations, 0


def _render_gaps(gaps: list[tuple[str, Gap]], cfg: DomainConfig,
                 by_field: dict[str, list[SourcedFact]]) -> tuple[str, list[str], int]:
    rows: list[list[str]] = []
    for document, gap in sorted(gaps, key=lambda g: (g[0], g[1].field_name)):
        rows.append([gap.field_name, document, gap.reason, gap.detail or "—"])

    declared: list[str] = []
    for spec in cfg.register.get("sections", []):
        declared.extend(spec.get("fields", []))
    for name in declared:
        if name not in by_field:
            rows.append([name, "—", "not stated in any document in the pile", "—"])

    if not rows:
        return "_Nothing was left unestablished._", [], 0
    return (_table(["Field", "Document", "Why", "Detail"], rows), [], len(rows))
