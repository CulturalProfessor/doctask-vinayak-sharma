"""What travels between nodes, and what deliberately does not.

The rule this file exists to enforce: **the checkpoint holds the run's position,
not its payload.** Every value below is a string, a number, or a list or dict of
those. No dataclass crosses a node boundary, and nothing here needs a custom
serialiser.

That is not tidiness. A checkpoint is read back by a *different process* after a
crash, possibly after a code change, and a state shape that depends on pickling
`SourcedFact` -> `ExtractedFact` -> `NormalisedValue` is a resume that breaks
quietly the first time one of those classes gains a field. Plain JSON cannot
break that way.

The cost is that `reconcile`, `compose` and `propose` each reload facts from the
database and re-run reconciliation rather than passing conflicts along. That is
a pure, deterministic function over rows already committed, and it takes
microseconds on a pile this size. It is also the same argument the incremental
update already makes for recomposing instead of predicting: a recomputed result
is evidence, a carried-along one is an assumption.

The one payload the state *does* carry is the composed register. That is on
purpose too -- commit must write the bytes a person approved, not bytes
recomputed after the fact and hoped to be equal. Commit checks the two against
each other and refuses if they differ.
"""
from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    # -- identity ---------------------------------------------------------
    run_id: str
    pile_id: str
    domain: str
    kind: str                       # full | incremental

    # -- position ---------------------------------------------------------
    sources: list[str]              # absolute paths this run was given
    queue: list[str]                # paths not yet understood
    current: str | None             # the path being understood right now

    # -- what ingest established ------------------------------------------
    documents: dict[str, str]       # filename -> document_id
    duplicates: list[str]           # filenames whose bytes were already held

    # -- what the per-document stages established -------------------------
    doc_types: dict[str, str]       # filename -> doc_type
    entity_keys: dict[str, str]     # filename -> engagement key
    fact_counts: dict[str, int]     # filename -> facts persisted
    gaps: list[dict[str, Any]]      # {document, field_name, reason, detail}
    quarantined: list[dict[str, str]]   # {document, note}
    escalated: list[dict[str, str]]     # {document, stage, note}

    # -- what the pile-level stages established ---------------------------
    conflict_count: int
    reconcile_path: str
    register: dict[str, Any] | None     # {title, sections: [...]}
    delta: dict[str, list[str]]         # {changed, unchanged, added}

    # -- the gate ---------------------------------------------------------
    proposal_count: int
    status: str
    note: str | None
    committed: dict[str, Any] | None


def new_state(run_id: str, pile_id: str, domain: str, kind: str,
              sources: list[str]) -> RunState:
    return RunState(
        run_id=run_id, pile_id=pile_id, domain=domain, kind=kind,
        sources=sources, queue=[], current=None,
        documents={}, duplicates=[], doc_types={}, entity_keys={},
        fact_counts={}, gaps=[], quarantined=[], escalated=[],
        conflict_count=0, reconcile_path="", register=None,
        delta={"changed": [], "unchanged": [], "added": []},
        proposal_count=0, status="running", note=None, committed=None,
    )


# --------------------------------------------------------- register shape --
#
# The register crosses node boundaries and lands in the checkpoint, so it goes
# as plain dicts. These two functions are the only place that shape is known.

def register_to_state(register) -> dict[str, Any]:
    return {
        "title": register.title,
        "sections": [
            {"key": s.key, "heading": s.heading, "ordinal": s.ordinal,
             "body": s.body, "content_hash": s.content_hash,
             "citations": list(s.citations), "gap_count": s.gap_count}
            for s in register.sections
        ],
    }


def register_from_state(data: dict[str, Any] | None):
    from app.stages.compose import Register, Section

    if not data:
        return None
    return Register(
        title=data["title"],
        sections=[Section(key=s["key"], heading=s["heading"], ordinal=s["ordinal"],
                          body=s["body"], content_hash=s["content_hash"],
                          citations=list(s.get("citations", [])),
                          gap_count=s.get("gap_count", 0))
                  for s in data["sections"]],
    )
