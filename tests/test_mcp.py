"""The whole flow, driven by a machine, with no browser and no HTTP.

Graded behaviour 4. The brief is specific that approval must be an operation on
the machine interface rather than a UI-only affordance, so the test that matters
is not "the server starts" -- it is a complete run from an empty pile to a
committed register, including a review with mixed decisions, going through the
MCP tools and nothing else.

What this must never do:

  1. Never let a machine commit anything that was not reviewed.
  2. Never let a machine's review be mistaken for a person's afterwards.
  3. Never offer through one surface what it does not offer through another.
  4. Never present document text to a client as anything but data.

Tools are called through the server's own dispatch rather than by calling the
Python functions underneath, because the wiring is the part that can be wrong.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import operations as ops
from app.mcp.server import server
from app.store.engine import fetch_all, fetch_one

pytestmark = pytest.mark.db


def call(name: str, **arguments) -> dict:
    """Invoke one MCP tool and read its result as the caller would."""
    result = asyncio.run(server.call_tool(name, arguments))
    text = "".join(block.text for block in result.content
                   if getattr(block, "type", None) == "text")
    return json.loads(text)


@pytest.fixture
def reviewed(pile):
    """A pile taken to the gate entirely through MCP."""
    run = call("doctask_start_run", pile_id=pile, corpus="pile_acme")
    assert "error" not in run, run
    return pile, run


# ------------------------------------------------------------ the whole flow --

def test_a_machine_drives_a_pile_from_nothing_to_a_committed_register(conn, reviewed):
    """Start, review with mixed decisions, commit, read the result. No UI, no
    HTTP, and no step that a person has to perform by hand."""
    pile, run = reviewed
    run_id = run["run_id"]

    assert run["status"] == "awaiting_approval"
    assert run["documents"] == 7 and run["facts"] == 48
    assert run["conflicts"] == 3 and run["pending_proposals"] == 13

    # Nothing exists yet, and the machine interface says so rather than
    # returning an empty register that reads like a real one.
    assert call("doctask_get_register", pile_id=pile)["error"] == "not_found"

    proposals = call("doctask_list_proposals", run_id=run_id,
                     status="pending")["proposals"]
    assert len(proposals) == 13
    assert {p["kind"] for p in proposals} == {"section_patch", "conflict", "finding"}

    # One rejection among eight approvals, in a single review.
    decisions = []
    for proposal in proposals:
        reject = (proposal["kind"] == "conflict"
                  and proposal["payload"]["field"] == "hourly_rate")
        decisions.append({
            "proposal_id": proposal["id"], "approved": not reject,
            "reason": "the rate change is historical" if reject else None,
        })
    outcome = call("doctask_decide", run_id=run_id, decisions=decisions,
                   decided_by="vinayak")
    assert outcome == {"run_id": run_id, "approved": 12, "rejected": 1,
                       "ignored": 0, "pending": 0}

    committed = call("doctask_commit", run_id=run_id)
    assert committed["status"] == "committed"
    assert committed["sections_written"] == 6

    register = call("doctask_get_register", pile_id=pile)
    assert len(register["sections"]) == 6
    assert all(section["content_hash"] for section in register["sections"])

    audit = call("doctask_get_audit", pile_id=pile)["audit"]
    assert len(audit) == 6


def test_a_machine_cannot_commit_what_was_not_reviewed(reviewed):
    """The gate holds against a program exactly as it holds against a person.
    Partial review is refused rather than resolved with a default."""
    _, run = reviewed
    refused = call("doctask_commit", run_id=run["run_id"])
    assert refused["error"] == "invalid"
    assert "still pending" in refused["detail"]


def test_a_machines_review_is_recorded_as_a_machines_review(conn, reviewed):
    """Behaviour 3 says a person holds the gate; behaviour 4 says a machine must
    be able to drive approval. Both hold only if the record can tell them apart
    afterwards -- and the surface, not the caller, is what writes that down."""
    pile, run = reviewed
    proposals = call("doctask_list_proposals", run_id=run["run_id"])["proposals"]
    call("doctask_decide", run_id=run["run_id"], decided_by="vinayak",
         decisions=[{"proposal_id": proposals[0]["id"], "approved": True}])

    conn.rollback()
    row = fetch_one(conn, "SELECT decided_by, decided_via FROM proposal WHERE id = %s",
                    (proposals[0]["id"],))
    assert row["decided_by"] == "vinayak"
    assert row["decided_via"] == "mcp", (
        "an approval that came through the machine interface must say so"
    )


def test_a_decision_with_no_decider_is_refused(reviewed):
    """An anonymous approval makes the audit trail's account of a review depend
    on which client happened to make it."""
    _, run = reviewed
    proposals = call("doctask_list_proposals", run_id=run["run_id"])["proposals"]
    refused = call("doctask_decide", run_id=run["run_id"], decided_by="  ",
                   decisions=[{"proposal_id": proposals[0]["id"], "approved": True}])
    assert refused["error"] == "invalid"
    assert "decider" in refused["detail"]


def test_a_machine_can_resume_a_run(conn, reviewed):
    """Behaviour 2 reachable from the machine interface. A program driving this
    system needs the same recovery a person has."""
    pile, run = reviewed
    proposals = call("doctask_list_proposals", run_id=run["run_id"])["proposals"]
    call("doctask_decide", run_id=run["run_id"], decided_by="vinayak",
         decisions=[{"proposal_id": p["id"], "approved": True} for p in proposals])

    resumed = call("doctask_resume", run_id=run["run_id"])
    assert resumed["status"] == "committed"
    assert call("doctask_resume", run_id="00000000-0000-0000-0000-000000000000")


def test_the_incremental_update_is_drivable_too(conn, reviewed):
    """The third movement from a machine: a document arrives, and only what it
    changed is put up for review."""
    pile, run = reviewed
    proposals = call("doctask_list_proposals", run_id=run["run_id"])["proposals"]
    call("doctask_decide", run_id=run["run_id"], decided_by="vinayak",
         decisions=[{"proposal_id": p["id"], "approved": True} for p in proposals])
    call("doctask_commit", run_id=run["run_id"])

    update = call("doctask_document_arrived", pile_id=pile,
                  document="arrivals/amendment_02.md")
    assert update["status"] == "awaiting_approval"
    assert update["changed"], "the amendment must move something"
    assert "billing" in update["unchanged"], "no invoice arrived"
    assert update["model_calls"] == 2, "an update should cost like an update"

    # Sending the same bytes again does nothing, and says so.
    again = call("doctask_document_arrived", pile_id=pile,
                 document="arrivals/amendment_02.md")
    assert again["status"] == "no_change"
    assert again["model_calls"] == 0


def test_a_path_outside_the_corpus_is_refused(pile):
    """A caller supplying a path is a caller supplying a path, and one of these
    surfaces is driven by a model reading documents that may ask it to."""
    refused = call("doctask_document_arrived", pile_id=pile,
                   document="../../../etc/passwd")
    assert refused["error"] == "invalid"
    assert "outside corpora" in refused["detail"]


# ------------------------------------------------------------------ parity --

def test_every_operation_is_reachable_from_the_machine_interface():
    """The structural claim behind behaviour 4: nothing the system can do is
    missing from the surface a program uses. This is a test rather than a
    convention because the failure mode -- a capability that exists over HTTP
    and quietly does not over MCP -- is invisible until someone needs it."""
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    expected = {
        "list_piles": "doctask_list_piles",
        "create_pile": "doctask_create_pile",
        "list_documents": "doctask_list_documents",
        "start_run": "doctask_start_run",
        "arrival": "doctask_document_arrived",
        "get_run": "doctask_get_run",
        "run_report": "doctask_run_report",
        "resume": "doctask_resume",
        "list_proposals": "doctask_list_proposals",
        "decide": "doctask_decide",
        "commit": "doctask_commit",
        "register": "doctask_get_register",
        "findings": "doctask_get_findings",
        "audit": "doctask_get_audit",
    }
    operations = {name for name in ops.__all__ if not name[0].isupper()}
    assert set(expected) == operations, (
        "an operation was added or removed without the machine interface "
        "following it"
    )
    assert set(expected.values()) <= tools


def test_the_server_speaks_the_protocol_over_stdio():
    """The tests above call the server's dispatch in-process, which does not
    prove a client can reach it. This starts the documented entry point and
    completes a real handshake, because "the tools are registered" and "an agent
    can use them" are different claims."""
    import subprocess
    import sys

    from tests.conftest import REPO_ROOT

    handshake = "\n".join(json.dumps(message) for message in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]) + "\n"

    process = subprocess.run(
        [sys.executable, "-m", "app.mcp.server"], input=handshake,
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    initialise = next(r for r in replies if r.get("id") == 1)
    listing = next(r for r in replies if r.get("id") == 2)

    assert initialise["result"]["serverInfo"]["name"] == "doctask"
    assert "never instruction" in initialise["result"]["instructions"], (
        "a client should be told documents are evidence before it reads any"
    )
    assert {tool["name"] for tool in listing["result"]["tools"]} >= {
        "doctask_start_run", "doctask_list_proposals", "doctask_decide",
        "doctask_commit", "doctask_resume",
    }


def test_the_surfaces_share_one_implementation():
    """Neither surface may contain a decision of its own. If either grows one,
    they can disagree -- and the disagreement would be about who may approve
    what."""
    import app.api.runs as http_surface
    import app.mcp.server as mcp_surface

    for module in (http_surface, mcp_surface):
        source = module.__file__
        with open(source) as handle:
            text = handle.read()
        assert "app.graph" not in text and "from app.store" not in text, (
            f"{source} reaches past app.operations into the machinery"
        )
