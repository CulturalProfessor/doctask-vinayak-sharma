"""Everything the system can be asked to do, in one place.

Graded behaviour 4: another program must be able to drive the whole flow with no
human clicking anything, and **approval is part of that flow**.

There are three surfaces over this system -- HTTP, MCP, and a review UI -- and
the requirement is not that each of them can do everything. It is that they are
the *same* operations, so that no capability can exist on one and be missing
from another, and no two of them can answer the same question differently. This
module is that guarantee made structural rather than promised: the surfaces
below it are argument parsing and error mapping, and none of them contains a
decision.

The repo already learned this lesson the expensive way. A full run and an
incremental update were two code paths, and a fix for entity identity landed on
one and not the other, silently. Two review surfaces would fail the same way and
the failure would be worse, because it would be about who is allowed to approve
what.

Errors are domain exceptions, not HTTP status codes. A surface translates them;
`PileBusy` becoming 409 is a fact about HTTP, not a fact about a busy pile.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain.config import ConfigError, load_domain
from app.graph import pipeline
from app.graph.locking import PileBusy
from app.llm.base import get_provider
from app.settings import REPO_ROOT
from app.stages import gate as gate_module
from app.stages.gate import Decision
from app.store import repository as repo
from app.store.engine import fetch_all, fetch_one, transaction

CORPORA = REPO_ROOT / "corpora"


class NotFound(LookupError):
    """The thing named does not exist."""


class Invalid(ValueError):
    """The request is well-formed but cannot be carried out as asked."""


__all__ = [
    "NotFound", "Invalid", "PileBusy",
    "list_piles", "create_pile", "list_documents",
    "start_run", "arrival", "get_run", "list_runs", "list_proposals", "decide",
    "commit", "resume", "run_report", "register", "audit", "findings",
]


# ------------------------------------------------------------------ piles --

# The statuses that mean a document is *not* part of the understanding: its
# format was refused, or it was quarantined for carrying instructions aimed at
# the system. Everything else -- 'ingested', 'classified', 'extracted' -- is a
# document that was read, at some point along the way.
#
# Named explicitly because the alternative was `status <> 'ingested'`, which
# counted pipeline *progress* as failure: a pile whose seven documents had all
# been classified reported "0 documents, 7 not read" while its register sat
# there composed from their facts. A read that succeeded must never be displayed
# as a gap -- gaps are output, and a fake one is as bad as a missing real one.
GAP_STATUSES = ("quarantined", "unsupported")


def list_piles() -> dict[str, Any]:
    with transaction() as conn:
        return {"piles": fetch_all(conn, """
            SELECT p.id, p.name, p.domain, p.created_at,
                   count(d.id) FILTER (WHERE NOT (d.status = ANY(%s))) AS documents,
                   count(d.id) FILTER (WHERE d.status = ANY(%s))       AS gaps
            FROM pile p LEFT JOIN document d ON d.pile_id = p.id
            GROUP BY p.id ORDER BY p.name
        """, (list(GAP_STATUSES), list(GAP_STATUSES)))}


def create_pile(name: str, domain: str = "vendor_contracts") -> dict[str, Any]:
    from app.ingest.ingest import ensure_pile

    _config(domain)
    with transaction() as conn:
        return {"pile_id": ensure_pile(conn, name, domain), "name": name,
                "domain": domain}


def list_documents(pile_id: str) -> dict[str, Any]:
    _require_pile(pile_id)
    with transaction() as conn:
        return {"pile_id": pile_id, "documents": fetch_all(conn, """
            SELECT d.id, d.filename, d.format, d.doc_type, d.status, d.ingest_note,
                   d.byte_size, d.ingested_at, count(pg.id) AS pages,
                   -- Answered here rather than by each surface, so that HTTP,
                   -- MCP and the review UI cannot disagree about whether a
                   -- document was read.
                   d.status = ANY(%s) AS is_gap
            FROM document d LEFT JOIN page pg ON pg.document_id = d.id
            WHERE d.pile_id = %s
            GROUP BY d.id ORDER BY d.filename
        """, (list(GAP_STATUSES), pile_id))}


# -------------------------------------------------------------------- runs --

def start_run(pile_id: str, corpus: str = "pile_acme",
              domain: str = "vendor_contracts") -> dict[str, Any]:
    """Ingest, understand, and halt at the gate. Commits nothing."""
    cfg = _config(domain)
    directory = _under_corpora(corpus)
    paths = sorted(p for p in directory.glob("*") if p.is_file()) \
        if directory.is_dir() else []
    if not paths:
        raise Invalid(f"no documents in corpora/{corpus}")
    _require_pile(pile_id)

    return _summarise(pipeline.run_understand(get_provider(), cfg, pile_id, paths))


def arrival(pile_id: str, document: str,
            domain: str = "vendor_contracts") -> dict[str, Any]:
    """A document lands. Produces a targeted update, and halts at the gate."""
    cfg = _config(domain)
    path = _under_corpora(document)
    if not path.is_file():
        raise NotFound(f"no document at corpora/{document}")
    _require_pile(pile_id)

    return _summarise(pipeline.run_incremental(get_provider(), cfg, pile_id, path))


def resume(run_id: str) -> dict[str, Any]:
    """Continue a run that stopped, wherever it stopped.

    Behaviour 2 as an operation rather than as something only a restarted
    container does on its own. A machine driving this system needs the same
    recovery a person has.
    """
    return _summarise(_resume(run_id))


def get_run(run_id: str) -> dict[str, Any]:
    with transaction() as conn:
        run = _require_run(conn, run_id)
        pending = len(repo.list_proposals(conn, run_id, status="pending"))
    return {"run": run, "pending_proposals": pending}


def list_runs(pile_id: str, status: str | None = None) -> dict[str, Any]:
    """This pile's runs, newest first.

    Exists so that a gate can be found again. A run halted for review is a piece
    of unfinished work that belongs to the pile, not to whichever client started
    it -- so a reviewer who closed the tab, and an agent that lost its run id,
    both have a way back to it. Without this the only thing standing between an
    open gate and an unreachable one is a browser refresh.
    """
    _require_pile(pile_id)
    with transaction() as conn:
        return {"pile_id": pile_id, "runs": repo.runs_for_pile(conn, pile_id, status)}


def run_report(run_id: str) -> dict[str, Any]:
    """Behaviour 10: what the run spent, stage by stage, and which paths it took."""
    with transaction() as conn:
        _require_run(conn, run_id)
        return repo.run_report(conn, run_id)


# -------------------------------------------------------------- the gate --

def list_proposals(run_id: str, status: str | None = None) -> dict[str, Any]:
    with transaction() as conn:
        _require_run(conn, run_id)
        return {"run_id": run_id,
                "proposals": repo.list_proposals(conn, run_id, status)}


def decide(run_id: str, decisions: list[dict[str, Any]], decided_by: str,
           decided_via: str = "direct") -> dict[str, Any]:
    """Approve and reject individual items in one review.

    Mixed decisions in a single call are the normal case, not a special one.

    `decided_by` is required and has no default: every surface has to say who is
    deciding, and an operation that let a caller stay anonymous would make the
    audit trail's account of a review depend on which client happened to make
    it.

    `decided_via` is set by the surface, not by the caller, and is the honest
    answer to the tension between behaviour 3 and behaviour 4. A person holds
    the gate; a machine must be able to drive the whole flow including approval.
    The system accepts both and records which one it was, so nobody can later
    read the trail and believe a person reviewed what an agent waved through.
    """
    if not str(decided_by or "").strip():
        raise Invalid("decided_by is required: a decision has to have a decider")
    if not decisions:
        raise Invalid("no decisions supplied")

    with transaction() as conn:
        _require_run(conn, run_id)
        counts = gate_module.decide(
            conn, run_id,
            [Decision(str(d["proposal_id"]), bool(d["approved"]), d.get("reason"))
             for d in decisions],
            decided_by=decided_by, decided_via=decided_via,
        )
        remaining = len(repo.list_proposals(conn, run_id, status="pending"))
    # `ignored` means a proposal was already decided. Reported rather than
    # hidden, so a caller is never told its decision landed when it did not.
    return {"run_id": run_id, **counts, "pending": remaining}


def commit(run_id: str) -> dict[str, Any]:
    """Resume the run past its gate, writing exactly what was approved."""
    with transaction() as conn:
        _require_run(conn, run_id)
        pending = len(repo.list_proposals(conn, run_id, status="pending"))
    if pending:
        raise Invalid(f"{pending} proposal(s) still pending; a run cannot commit "
                      f"while any item is undecided")

    result = _resume(run_id)
    return {"run_id": run_id, "status": result.status, **(result.committed or {})}


# ------------------------------------------------------------- the output --

def register(pile_id: str, version: int | None = None) -> dict[str, Any]:
    with transaction() as conn:
        sections = repo.sections_for_version(conn, pile_id, version)
    if not sections:
        raise NotFound("no committed register for this pile")
    return {"pile_id": pile_id, "sections": sections}


def findings(pile_id: str, outcome: str | None = None) -> dict[str, Any]:
    """What the playbook said about this pile, including where it said nothing.

    Returns every rule's current answer, not only the broken ones. "Eight rules
    were checked and six held" is a different claim from "two problems were
    found", and only the first is worth trusting -- so the satisfied and the
    unjudgeable are part of the answer rather than filtered out of it.
    """
    _require_pile(pile_id)
    with transaction() as conn:
        rows = repo.findings_for_pile(conn, pile_id, outcome)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {"pile_id": pile_id, "counts": counts, "findings": rows}


def audit(pile_id: str) -> dict[str, Any]:
    """What changed, when, and because of which source."""
    _require_pile(pile_id)
    with transaction() as conn:
        return {"pile_id": pile_id, "audit": repo.audit_trail(conn, pile_id)}


# ---------------------------------------------------------------- shared --

def _resume(run_id: str) -> pipeline.RunResult:
    """Resume, translating the pipeline's vocabulary into this module's.

    `pipeline.resume` raises a bare `LookupError` for both an unknown run and a
    run with no checkpoint. Letting that reach a surface means every surface has
    to know about pipeline exceptions, and the one that forgets turns a missing
    run into a 500. Converted once, here.
    """
    try:
        return pipeline.resume(get_provider(), run_id=run_id)
    except NotFound:
        raise
    except LookupError as exc:
        raise NotFound(str(exc)) from exc


def _config(domain: str):
    try:
        return load_domain(domain)
    except ConfigError as exc:
        raise Invalid(str(exc))


def _require_pile(pile_id: str) -> None:
    with transaction() as conn:
        if not fetch_one(conn, "SELECT id FROM pile WHERE id = %s", (pile_id,)):
            raise NotFound(f"no pile {pile_id}")


def _require_run(conn, run_id: str) -> dict[str, Any]:
    run = repo.get_run(conn, run_id)
    if not run:
        raise NotFound(f"no run {run_id}")
    return run


def _under_corpora(relative: str) -> Path:
    """Resolve a caller-supplied path, refusing anything outside `corpora/`.

    Both surfaces take a path from a caller, and one of them is driven by a
    model reading documents that may themselves contain instructions. Documents
    are data, never instructions -- so `../../etc/passwd` is refused here, once,
    rather than in each surface's argument handling where it would eventually be
    forgotten in one of them.
    """
    candidate = (CORPORA / relative).resolve()
    if candidate != CORPORA and CORPORA not in candidate.parents:
        raise Invalid(f"{relative!r} is outside corpora/")
    return candidate


def _summarise(result: pipeline.RunResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "note": result.note,
        "documents": len(result.documents),
        "duplicates": result.duplicates,
        "facts": result.fact_count,
        "gaps": len(result.gaps),
        "conflicts": len(result.conflicts),
        "quarantined": [row["document"] for row in result.quarantined],
        "escalated": [row["document"] for row in result.escalated],
        "changed": result.delta.changed,
        "unchanged": result.delta.unchanged,
        "added": result.delta.added,
        "model_calls": result.model_calls,
        "replayed_calls": result.replayed_calls,
        "pending_proposals": len(result.gate.pending),
    }
