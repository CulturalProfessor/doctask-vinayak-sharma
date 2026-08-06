"""Run and gate operations over HTTP.

Graded behaviour 4: another program must be able to drive the whole flow with no
human clicking anything, and **approval is part of that flow**. Every operation
the review interface will offer exists here first, so the UI is one client of
this API rather than a privileged path into the system.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.domain.config import ConfigError, load_domain
from app.graph.pipeline import run_understand
from app.llm.base import ProviderError, get_provider
from app.settings import REPO_ROOT
from app.stages import gate as gate_module
from app.stages.gate import Decision
from app.store import repository as repo
from app.store.engine import fetch_one, transaction

router = APIRouter()

# Registers are held per run between composing and committing. They are
# reproducible from the facts, so losing them to a restart costs a recompose
# rather than any committed work.
_REGISTERS: dict[str, object] = {}


class StartRun(BaseModel):
    pile_id: str
    corpus: str = Field(default="pile_acme", description="directory under corpora/")
    domain: str = "vendor_contracts"


class DecisionIn(BaseModel):
    proposal_id: str
    approved: bool
    reason: str | None = None


class DecideRequest(BaseModel):
    decisions: list[DecisionIn]
    decided_by: str = "api"


@router.post("/runs")
def start_run(body: StartRun) -> dict:
    """Ingest, understand, and halt at the gate. Commits nothing."""
    try:
        cfg = load_domain(body.domain)
    except ConfigError as exc:
        raise HTTPException(400, str(exc))

    directory = REPO_ROOT / "corpora" / body.corpus
    paths = sorted(p for p in directory.glob("*") if p.is_file()) if directory.is_dir() else []
    if not paths:
        raise HTTPException(400, f"no documents in {directory}")

    with transaction() as conn:
        if not fetch_one(conn, "SELECT id FROM pile WHERE id = %s", (body.pile_id,)):
            raise HTTPException(404, f"no pile {body.pile_id}")
        try:
            result = run_understand(conn, get_provider(), cfg, body.pile_id, paths)
        except ProviderError as exc:
            raise HTTPException(502, f"model provider failed: {exc}")

    _REGISTERS[result.run_id] = result.register
    report = result.report
    return {
        "run_id": result.run_id,
        "status": "awaiting_approval",
        "documents": len(result.ingested),
        "facts": len(report.facts),
        "gaps": len(report.gaps),
        "conflicts": len(report.reconciliation.conflicts),
        "quarantined": [name for name, _ in report.quarantined],
        "escalated": [name for name, _ in report.escalated],
        "pending_proposals": len(result.gate.pending),
    }


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
    register = _REGISTERS.get(run_id)
    if register is None:
        raise HTTPException(
            409, "this run's register is not in memory; re-run to recompose it")
    with transaction() as conn:
        if not (run := repo.get_run(conn, run_id)):
            raise HTTPException(404, f"no run {run_id}")
        try:
            result = gate_module.commit(conn, str(run["pile_id"]), run_id, register)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    return {"run_id": run_id, **result}


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
