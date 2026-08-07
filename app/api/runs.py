"""Run and gate operations over HTTP.

Graded behaviour 4: another program must be able to drive the whole flow with no
human clicking anything, and **approval is part of that flow**. Every operation
the review interface will offer exists here first, so the UI is one client of
this API rather than a privileged path into the system.

Nothing here holds run state in memory. It used to hold the composed register in
a module-level dict, and `POST /commit` after a restart answered "this run's
register is not in memory; re-run to recompose it" -- which is a server telling a
caller to redo finished work because the server forgot. The register now lives in
the run's checkpoint, so committing is resuming, and a restart between the review
and the commit costs nothing.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.domain.config import ConfigError, load_domain
from app.graph import pipeline
from app.llm.base import ProviderError, ProviderUnavailable, get_provider
from app.settings import REPO_ROOT
from app.stages import gate as gate_module
from app.stages.gate import Decision
from app.store import repository as repo
from app.store.engine import fetch_one, transaction

router = APIRouter()


class StartRun(BaseModel):
    pile_id: str
    corpus: str = Field(default="pile_acme", description="directory under corpora/")
    domain: str = "vendor_contracts"


class ArrivalRun(BaseModel):
    pile_id: str
    document: str = Field(description="path under corpora/, e.g. arrivals/amendment_02.md")
    domain: str = "vendor_contracts"


class DecisionIn(BaseModel):
    proposal_id: str
    approved: bool
    reason: str | None = None


class DecideRequest(BaseModel):
    decisions: list[DecisionIn]
    decided_by: str = "api"


def _summary(result: pipeline.RunResult) -> dict:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "note": result.note,
        "documents": len(result.documents),
        "duplicates": result.duplicates,
        "facts": result.facts,
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


def _load(domain: str):
    try:
        return load_domain(domain)
    except ConfigError as exc:
        raise HTTPException(400, str(exc))


def _require_pile(pile_id: str) -> None:
    with transaction() as conn:
        if not fetch_one(conn, "SELECT id FROM pile WHERE id = %s", (pile_id,)):
            raise HTTPException(404, f"no pile {pile_id}")


@router.post("/runs")
def start_run(body: StartRun) -> dict:
    """Ingest, understand, and halt at the gate. Commits nothing."""
    cfg = _load(body.domain)
    directory = REPO_ROOT / "corpora" / body.corpus
    paths = sorted(p for p in directory.glob("*") if p.is_file()) if directory.is_dir() else []
    if not paths:
        raise HTTPException(400, f"no documents in {directory}")
    _require_pile(body.pile_id)

    try:
        result = pipeline.run_understand(get_provider(), cfg, body.pile_id, paths)
    except ProviderUnavailable as exc:
        # The deployment cannot reach a model at all. Reporting this as a
        # per-document escalation would produce a run that looks healthy and
        # understood nothing.
        raise HTTPException(503, f"model provider unavailable: {exc}")
    except ProviderError as exc:
        raise HTTPException(502, f"model provider failed: {exc}")
    return _summary(result)


@router.post("/arrivals")
def arrival(body: ArrivalRun) -> dict:
    """A document lands. Produces a targeted update, and halts at the gate."""
    cfg = _load(body.domain)
    path = (REPO_ROOT / "corpora" / body.document).resolve()
    if not path.is_file() or REPO_ROOT / "corpora" not in path.parents:
        raise HTTPException(400, f"no document at corpora/{body.document}")
    _require_pile(body.pile_id)

    try:
        result = pipeline.run_incremental(get_provider(), cfg, body.pile_id, path)
    except ProviderUnavailable as exc:
        raise HTTPException(503, f"model provider unavailable: {exc}")
    except ProviderError as exc:
        raise HTTPException(502, f"model provider failed: {exc}")
    return _summary(result)


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with transaction() as conn:
        run = repo.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, f"no run {run_id}")
        pending = len(repo.list_proposals(conn, run_id, status="pending"))
    return {"run": run, "pending_proposals": pending}


@router.get("/runs/{run_id}/proposals")
def list_proposals(run_id: str, status: str | None = None) -> dict:
    with transaction() as conn:
        if not repo.get_run(conn, run_id):
            raise HTTPException(404, f"no run {run_id}")
        proposals = repo.list_proposals(conn, run_id, status)
    return {"run_id": run_id, "proposals": proposals}


@router.post("/runs/{run_id}/decide")
def decide(run_id: str, body: DecideRequest) -> dict:
    """Approve and reject individual items in one review.

    Mixed decisions in a single call are the normal case, not a special one.
    """
    with transaction() as conn:
        if not repo.get_run(conn, run_id):
            raise HTTPException(404, f"no run {run_id}")
        counts = gate_module.decide(
            conn, run_id,
            [Decision(d.proposal_id, d.approved, d.reason) for d in body.decisions],
            decided_by=body.decided_by,
        )
        remaining = len(repo.list_proposals(conn, run_id, status="pending"))
    # `ignored` means a proposal was already decided. Reported rather than
    # hidden, so a caller is never told its decision landed when it did not.
    return {"run_id": run_id, **counts, "pending": remaining}


@router.post("/runs/{run_id}/commit")
def commit(run_id: str) -> dict:
    """Resume the run past its gate, writing exactly what was approved."""
    with transaction() as conn:
        run = repo.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, f"no run {run_id}")
        pending = len(repo.list_proposals(conn, run_id, status="pending"))
    if pending:
        raise HTTPException(
            409, f"{pending} proposal(s) still pending; a run cannot commit while "
                 f"any item is undecided")

    try:
        result = pipeline.resume(get_provider(), run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": run_id, "status": result.status, **(result.committed or {})}


@router.post("/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    """Continue a run that stopped. Behaviour 2, as an operation.

    A machine driving this system needs the same recovery a person has, so
    resumption is an API call rather than something only a restarted container
    does on its own.
    """
    try:
        result = pipeline.resume(get_provider(), run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _summary(result)


@router.get("/runs/{run_id}/report")
def report(run_id: str) -> dict:
    """Behaviour 10: what the run spent, stage by stage, and which paths it took."""
    with transaction() as conn:
        if not repo.get_run(conn, run_id):
            raise HTTPException(404, f"no run {run_id}")
        return repo.run_report(conn, run_id)


@router.get("/piles/{pile_id}/register")
def register(pile_id: str, version: int | None = None) -> dict:
    with transaction() as conn:
        sections = repo.sections_for_version(conn, pile_id, version)
    if not sections:
        raise HTTPException(404, "no committed register for this pile")
    return {"pile_id": pile_id, "sections": sections}


@router.get("/piles/{pile_id}/audit")
def audit(pile_id: str) -> dict:
    """What changed, when, and because of which source."""
    with transaction() as conn:
        return {"pile_id": pile_id, "audit": repo.audit_trail(conn, pile_id)}
