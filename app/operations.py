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
    "list_piles", "create_pile", "list_documents", "corpora", "upload",
    "start_run", "arrival", "get_run", "list_runs", "list_proposals", "decide",
    "commit", "resume", "run_report", "register", "audit", "findings",
    "search", "entities", "watch_status",
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


# --------------------------------------------------------------- corpora --

# Folders and files a caller can be offered instead of being asked to type a
# path. Anything bigger than this is a corpus nobody is picking through a list,
# and returning it all would turn a picker into a download.
MAX_CORPORA_ENTRIES = 500


def corpora() -> dict[str, Any]:
    """What is available to read, so nobody has to guess a path.

    `start_run` takes a folder name and `arrival` takes a file path, and until
    this existed the only way to learn either was to already know it. That is a
    poor deal for a person and a worse one for an agent: behaviour 4 says a
    machine can drive the whole flow, and a flow whose first step is "supply a
    string you cannot discover" is one an agent can only complete if somebody
    tells it the answer out of band.

    Unsupported files are listed rather than filtered out, marked. A picker that
    silently omits `notes.rtf` leaves someone wondering where their document
    went; one that shows it greyed out with the reason has answered the question
    before it was asked. That is the same rule the ingest stage follows for a
    format it cannot read.
    """
    from app.ingest.formats import UnsupportedFormat, detect_format

    folders: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    if not CORPORA.is_dir():
        return {"root": "corpora", "folders": [], "files": []}

    for directory in sorted(p for p in CORPORA.iterdir() if p.is_dir()):
        entries = sorted(p for p in directory.rglob("*") if p.is_file()
                         and not p.name.startswith("."))
        readable = 0
        for path in entries[:MAX_CORPORA_ENTRIES]:
            try:
                fmt: str | None = detect_format(path)
                note = None
            except UnsupportedFormat as exc:
                fmt, note = None, str(exc)
            if fmt:
                readable += 1
            files.append({
                "path": str(path.relative_to(CORPORA)),
                "folder": directory.name,
                "name": path.name,
                "bytes": path.stat().st_size,
                "format": fmt,
                "supported": fmt is not None,
                "note": note,
            })
        folders.append({
            "name": directory.name,
            "files": len(entries),
            "readable": readable,
        })

    return {"root": "corpora", "folders": folders, "files": files}


# Where a document sent from someone's own machine is kept.
#
# Not the watched folder. If an upload landed there, the watcher would see the
# same bytes this call is already reading and race it for the pile, and one of
# the two would report a busy pile for a document nobody else touched.
#
# Under corpora/ rather than somewhere private because the run reads the file
# from disk, and because `document.uri` should point at something that still
# exists afterwards. The contents are gitignored: a document someone uploads may
# be a real contract, and nobody's real paperwork belongs in this repository.
UPLOADS = CORPORA / "uploads"

# Big enough for a scanned agreement, small enough that one request cannot fill
# the disk. A refusal names the limit rather than failing at write time.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def upload(pile_id: str, filename: str, data: bytes,
           domain: str = "vendor_contracts") -> dict[str, Any]:
    """Take a document from the caller's machine, store it, and read it.

    One operation rather than two because "the file is uploaded" and "the file
    has been read" are different claims, and an upload that only did the first
    left a caller looking at a success message for a document the register had
    never heard of. This stores the bytes and then runs the same `arrival` that
    `POST /arrivals`, the MCP tool and the watched folder run, so an uploaded
    document reaches the gate by exactly the path every other document takes.
    """
    _require_pile(pile_id)
    name = Path(filename or "").name.strip()
    if not name or name.startswith("."):
        raise Invalid("a document needs a filename")
    if not data:
        raise Invalid(f"{name} is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise Invalid(f"{name} is {len(data) // 1024 // 1024} MB; the limit is "
                      f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    UPLOADS.mkdir(parents=True, exist_ok=True)
    target = _free_path(UPLOADS / name, data)
    if not target.exists():
        target.write_bytes(data)

    result = arrival(pile_id, str(target.relative_to(CORPORA)), domain)
    return {"stored_as": str(target.relative_to(CORPORA)),
            "bytes": len(data), **result}


def _free_path(preferred: Path, data: bytes) -> Path:
    """A path these bytes can occupy without displacing anything.

    Same name, same bytes: reuse it, and ingest will report the document as one
    the pile already holds. Same name, different bytes: take a new name rather
    than overwrite. A document already in a pile is cited by character offsets
    into the text that was read from it, so replacing the file under it would
    leave every citation pointing at a document that no longer says what was
    quoted -- the register would still be internally consistent and would no
    longer be checkable, which is the worse of the two failures.
    """
    from app.ingest.ingest import sha256_bytes

    if not preferred.exists() or preferred.read_bytes() == data:
        return preferred
    digest = sha256_bytes(data)[:8]
    return preferred.with_name(f"{preferred.stem}-{digest}{preferred.suffix}")


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


# ------------------------------------------------------------- retrieval --

# A hard ceiling on how much of the pile one query can pull back. Not a
# pagination story: retrieval here is for checking a specific question against
# the sources, and a caller asking for five hundred passages is dumping the
# corpus rather than searching it.
MAX_SEARCH_RESULTS = 50

# Below this the hits are noise, and the number is a property of the embedder
# rather than a preference.
#
# Character trigrams mean any two English strings share *something*, so
# similarity never reaches zero and a floor is not optional. Measured on the
# shipped corpus: a query with no real answer in the pile ("certificate of
# insurance") scores 0.15-0.18 against unrelated headings -- "NOTICE OF
# NON-RENEWAL", "AMENDMENT NO. 2" -- purely on shared letters, while the
# weakest genuine lexical match observed ("four hundred (400) hours" for a query
# about hourly rates) scores 0.29. 0.25 sits in that gap.
#
# This was 0.15, which let the noise band through, and the cost was not a
# slightly untidy list: three unrelated headings rendered under "passages found"
# is the search presenting garbage as evidence, which is worse than returning
# nothing. Anything that survives this floor is still shown with its score, and
# the UI marks a weak best-match as weak, because one threshold cannot carry
# the whole claim.
MIN_SEARCH_SIMILARITY = 0.25


def search(pile_id: str, query: str, limit: int = 8) -> dict[str, Any]:
    """The passages in this pile that read most like `query`.

    Returns sources with their exact document, page and character offsets --
    the same provenance every other claim in this system carries, so a hit can
    be checked the same way a register value can.

    Two things this deliberately does not do. It does not answer the question:
    there is no model in this path, no summary, and no ranking of what the
    passages *mean*. And it does not treat an empty result as a finding. The
    embedder is lexical, so text that says the same thing in different words
    will not be found, and a caller that reads "no hits" as "the pile does not
    cover this" would be inventing exactly the kind of unsupported claim
    behaviour 5 forbids. `index_status` is returned alongside so a caller can
    tell an empty pile from an empty answer.
    """
    from app.retrieval import search as retrieval

    _require_pile(pile_id)
    text = (query or "").strip()
    if not text:
        raise Invalid("a search needs a query")
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))

    with transaction() as conn:
        hits = retrieval.search_spans(conn, pile_id, text, limit=limit,
                                      min_similarity=MIN_SEARCH_SIMILARITY)
        index = retrieval.index_status(conn, pile_id)
    return {"pile_id": pile_id, "query": text, "results": hits,
            "index": index,
            "note": ("similarity search over span text; these are sources, not "
                     "an answer, and an empty result is weak evidence of absence "
                     "because the embedder matches wording rather than meaning")}


def entities(pile_id: str) -> dict[str, Any]:
    """The engagements this pile knows, under the names it first met them by.

    What entity resolution compares a new document against, made visible --
    because "why did this document escalate?" is answered by this list.
    """
    from app.retrieval import search as retrieval

    _require_pile(pile_id)
    with transaction() as conn:
        return {"pile_id": pile_id, "entities": retrieval.entity_names(conn, pile_id)}


# ----------------------------------------------------------- the watcher --

def watch_status() -> dict[str, Any]:
    """What the watched location is, and what has arrived through it.

    An operation rather than a log line. A watcher whose state can only be read
    by tailing stderr cannot be checked by the machine driving this system, and
    "the inbox is empty" and "the watcher never started" look identical from
    outside.
    """
    from app import watch

    return watch.status()


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
        # The whole row, not just the filename. An escalation is a question put
        # to a person -- "does this document belong to the engagement we already
        # know, under a different spelling?" -- and the note is the question.
        # Reducing it to a filename left every surface able to say *that* a
        # document was set aside and none of them able to say why, which turns
        # the one outcome that needs a human into the one outcome nobody can
        # act on.
        "escalated": [
            {"document": row["document"], "stage": row.get("stage"),
             "note": row.get("note") or ""}
            for row in result.escalated
        ],
        "changed": result.delta.changed,
        "unchanged": result.delta.unchanged,
        "added": result.delta.added,
        "model_calls": result.model_calls,
        "replayed_calls": result.replayed_calls,
        "pending_proposals": len(result.gate.pending),
    }
