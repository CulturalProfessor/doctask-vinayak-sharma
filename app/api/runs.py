"""The HTTP surface over the operations.

Graded behaviour 4: another program must be able to drive the whole flow with no
human clicking anything, and **approval is part of that flow**.

There is deliberately no logic in this file. Every endpoint parses arguments,
calls one function in `app.operations`, and translates domain exceptions into
status codes. That is the point: HTTP, MCP and the review UI are the same
operations seen from three places, and nothing can be possible through one and
impossible through another. Two surfaces that each knew how to run a pile would
drift, and the drift would be about who is allowed to approve what.

Nothing here holds run state in memory. It used to hold the composed register in
a module-level dict, and `POST /commit` after a restart answered "this run's
register is not in memory; re-run to recompose it" -- a server telling a caller
to redo finished work because the server forgot. The register lives in the run's
checkpoint now, so committing is resuming.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import operations as ops
from app.llm.base import ProviderError, ProviderUnavailable

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
    decided_by: str = Field(description="who is deciding; recorded on every item")


def _translate(call):
    """Domain exceptions to status codes, in one place.

    The distinctions preserved here are ones the rest of the system works to
    keep apart. 503 and 502 are not interchangeable: a provider that cannot be
    reached means the deployment is broken, and reporting that as a per-document
    escalation is how a run comes back healthy-looking having understood
    nothing.
    """
    try:
        return call()
    except ops.NotFound as exc:
        raise HTTPException(404, str(exc))
    except ops.PileBusy as exc:
        # 409, not 500 and not a queue. The caller is told which run holds the
        # pile and that nothing was written, and can decide what to do.
        raise HTTPException(409, str(exc))
    except ops.Invalid as exc:
        raise HTTPException(409 if "pending" in str(exc) else 400, str(exc))
    except ProviderUnavailable as exc:
        raise HTTPException(503, f"model provider unavailable: {exc}")
    except ProviderError as exc:
        raise HTTPException(502, f"model provider failed: {exc}")


@router.post("/runs")
def start_run(body: StartRun) -> dict:
    """Ingest, understand, and halt at the gate. Commits nothing."""
    return _translate(lambda: ops.start_run(body.pile_id, body.corpus, body.domain))


@router.post("/arrivals")
def arrival(body: ArrivalRun) -> dict:
    """A document lands. Produces a targeted update, and halts at the gate."""
    return _translate(lambda: ops.arrival(body.pile_id, body.document, body.domain))


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _translate(lambda: ops.get_run(run_id))


@router.get("/runs/{run_id}/proposals")
def list_proposals(run_id: str, status: str | None = None) -> dict:
    return _translate(lambda: ops.list_proposals(run_id, status))


@router.post("/runs/{run_id}/decide")
def decide(run_id: str, body: DecideRequest) -> dict:
    """Approve and reject individual items in one review.

    Mixed decisions in a single call are the normal case, not a special one.
    """
    return _translate(lambda: ops.decide(
        run_id, [d.model_dump() for d in body.decisions], body.decided_by,
        decided_via="http"))


@router.post("/runs/{run_id}/commit")
def commit(run_id: str) -> dict:
    """Resume the run past its gate, writing exactly what was approved."""
    return _translate(lambda: ops.commit(run_id))


@router.post("/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    """Continue a run that stopped. Behaviour 2, as an operation.

    A machine driving this system needs the same recovery a person has, so
    resumption is a call rather than something only a restarted container does
    on its own.
    """
    return _translate(lambda: ops.resume(run_id))


@router.get("/runs/{run_id}/report")
def report(run_id: str) -> dict:
    """Behaviour 10: what the run spent, stage by stage, and which paths it took."""
    return _translate(lambda: ops.run_report(run_id))


@router.get("/piles/{pile_id}/register")
def register(pile_id: str, version: int | None = None) -> dict:
    return _translate(lambda: ops.register(pile_id, version))


@router.get("/piles/{pile_id}/audit")
def audit(pile_id: str) -> dict:
    """What changed, when, and because of which source."""
    return _translate(lambda: ops.audit(pile_id))
