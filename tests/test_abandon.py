"""Ending a run that will never finish, without erasing the work it did.

Some runs cannot be completed and cannot be resumed: the document one was
part-way through was deleted for good, an arrival turns out to have been a
mistake, a reviewer decides the pile should be read again from scratch. Before
this operation existed such a run sat at 'running' forever, counted as work in
progress, offering a resume that refused every time.

The dangerous way to build this is a delete, and every test here exists to hold
the line against it. A run is not a task; it is a record of work that actually
took place. Its facts are in the pile, its model calls are what make the cost
report add up, and its documents are cited by registers other runs committed.
Deleting the row would take all of that with it and leave the pile holding facts
whose provenance had been erased -- which is the exact failure this system is
built to prevent, performed on itself by its own cleanup button.

So the rules under test are:

  1. Nothing an abandoned run wrote is removed. Not its facts, not its costs,
     not its proposals, not its documents.
  2. Its proposals stay *undecided*. Marking them rejected would put words in a
     reviewer's mouth and make the audit trail describe a review that never
     happened.
  3. It cannot afterwards be resumed, reviewed or committed, and the refusal
     says who ended it and why.
  4. A finished run cannot be abandoned. History does not get rewritten.
  5. A run that is genuinely alive cannot be ended from underneath, because it
     holds its pile and something is still writing.
  6. The pile itself is untouched: it can be read again immediately.
"""
from __future__ import annotations

import pytest

from app import operations as ops
from app.graph.locking import hold_pile
from app.store import repository as repo
from app.store.engine import connect

pytestmark = pytest.mark.db


def _stopped(pile: str) -> str:
    """A run halted at the gate, which is where most abandonable runs are."""
    started = ops.start_run(pile, "pile_acme")
    assert started["status"] == "awaiting_approval"
    return started["run_id"]


# --------------------------------------------------- nothing is thrown away --

def test_an_abandoned_run_keeps_every_trace_of_what_it_did(conn, pile):
    """The whole reason this is a status and not a DELETE."""
    run_id = _stopped(pile)

    before = {
        "facts": len(repo.facts_for_pile(conn, pile)),
        "events": len(repo.stage_events(conn, run_id)),
        "proposals": len(repo.list_proposals(conn, run_id)),
        "documents": len(repo.documents_for_pile(conn, pile)),
    }
    assert before["facts"] and before["events"] and before["proposals"]

    ops.abandon(run_id, abandoned_by="vinayak", reason="reading this pile again from scratch")

    assert len(repo.facts_for_pile(conn, pile)) == before["facts"]
    assert len(repo.list_proposals(conn, run_id)) == before["proposals"]
    assert len(repo.documents_for_pile(conn, pile)) == before["documents"]
    # One more event than before: the abandonment itself is part of the history.
    assert len(repo.stage_events(conn, run_id)) == before["events"] + 1

    run = repo.get_run(conn, run_id)
    assert run["status"] == "abandoned"
    assert run["abandoned_by"] == "vinayak"
    assert run["abandon_reason"] == "reading this pile again from scratch"
    assert run["ended_at"] is not None


def test_the_cost_of_an_abandoned_run_is_still_reported(conn, pile):
    """Behaviour 10 does not get to forget the runs that did not work out. A
    system that only counts the successful ones under-reports what it spent, and
    it under-reports in the direction that flatters it."""
    run_id = _stopped(pile)
    spent = ops.run_report(run_id)

    ops.abandon(run_id, abandoned_by="vinayak", reason="not needed")

    after = ops.run_report(run_id)
    assert after["totals"] == spent["totals"]
    assert after["totals"]["tokens_in"] > 0 and after["totals"]["cost_usd"] >= 0


def test_the_proposals_are_left_undecided_and_not_rejected(conn, pile):
    """"Nobody decided these" and "a reviewer rejected these" are different
    facts about a pile, and only one of them is true."""
    run_id = _stopped(pile)
    pending = len(repo.list_proposals(conn, run_id, status="pending"))
    assert pending > 0

    outcome = ops.abandon(run_id, abandoned_by="vinayak", reason="mistaken arrival")

    assert outcome["left_undecided"] == pending
    assert len(repo.list_proposals(conn, run_id, status="pending")) == pending
    assert repo.list_proposals(conn, run_id, status="rejected") == []


def test_the_ending_is_in_the_runs_own_history(conn, pile):
    """Findable from the run's timeline, not only from a column someone has to
    know to look at."""
    run_id = _stopped(pile)
    ops.abandon(run_id, abandoned_by="priya", reason="superseded by amendment 3")

    last = repo.stage_events(conn, run_id)[-1]
    assert last["stage"] == "abandon"
    assert last["detail"]["by"] == "priya"
    assert last["detail"]["reason"] == "superseded by amendment 3"


# ------------------------------------------------------ and nothing resumes --

def test_an_abandoned_run_cannot_be_resumed_reviewed_or_committed(conn, pile):
    """Three doors into the same run, and all three have to be shut. The one
    that stays open is the one that quietly resurrects a run somebody
    deliberately ended."""
    run_id = _stopped(pile)
    proposals = repo.list_proposals(conn, run_id, status="pending")
    ops.abandon(run_id, abandoned_by="vinayak", reason="the source document is gone")

    for call in (
        lambda: ops.resume(run_id),
        lambda: ops.commit(run_id),
        lambda: ops.decide(run_id,
                           [{"proposal_id": str(proposals[0]["id"]), "approved": True}],
                           decided_by="vinayak"),
    ):
        with pytest.raises(ops.Invalid) as raised:
            call()
        # The refusal carries the account of what happened, because "cannot be
        # resumed" on its own sends someone looking for a bug.
        assert "abandoned by vinayak" in str(raised.value)
        assert "the source document is gone" in str(raised.value)


def test_a_committed_run_cannot_be_abandoned(conn, pile):
    """A register exists and people are relying on it. Ending the run that
    produced it would describe a deliverable as work that was called off."""
    run_id = _stopped(pile)
    for proposal in repo.list_proposals(conn, run_id, status="pending"):
        ops.decide(run_id, [{"proposal_id": str(proposal["id"]), "approved": True}],
                   decided_by="vinayak")
    ops.commit(run_id)

    with pytest.raises(ops.Invalid, match="committed"):
        ops.abandon(run_id, abandoned_by="vinayak", reason="changed my mind")
    assert repo.get_run(conn, run_id)["status"] == "committed"


def test_abandoning_the_same_run_twice_refuses(pile):
    run_id = _stopped(pile)
    ops.abandon(run_id, abandoned_by="vinayak", reason="first")

    with pytest.raises(ops.Invalid, match="abandoned by vinayak"):
        ops.abandon(run_id, abandoned_by="someone else", reason="second")


def test_a_run_that_does_not_exist_is_not_found():
    with pytest.raises(ops.NotFound):
        ops.abandon("00000000-0000-0000-0000-000000000000",
                    abandoned_by="vinayak", reason="nothing there")


# ------------------------------------------------------ who ended it, and why --

@pytest.mark.parametrize("by,reason", [("", "a reason"), ("   ", "a reason"),
                                       ("vinayak", ""), ("vinayak", "  ")])
def test_an_ending_needs_an_author_and_an_explanation(pile, by, reason):
    """The one operation whose entire justification is that history is kept
    does not get to write an anonymous, unexplained entry into it."""
    run_id = _stopped(pile)
    with pytest.raises(ops.Invalid):
        ops.abandon(run_id, abandoned_by=by, reason=reason)
    assert ops.get_run(run_id)["run"]["status"] == "awaiting_approval"


# ------------------------------------------------- a live run is untouchable --

def test_a_run_that_is_genuinely_working_cannot_be_ended_underneath_it(pile):
    """The case that would be a disaster if it were allowed.

    A run in flight holds its pile, and the two things it is in the middle of --
    writing facts and setting its own status -- would both race an abandonment.
    So this takes the pile the same way any writer does and is refused when
    something else has it, which also means the check costs no new machinery and
    cannot drift out of step with the locking the rest of the system uses.
    """
    run_id = _stopped(pile)

    with connect() as holder, hold_pile(holder, pile):
        with pytest.raises(ops.PileBusy):
            ops.abandon(run_id, abandoned_by="vinayak", reason="racing the run")

    # Released, and the operation works again. Otherwise this test would pass
    # for a system that had simply broken abandonment.
    ops.abandon(run_id, abandoned_by="vinayak", reason="now that the pile is free")
    assert ops.get_run(run_id)["run"]["status"] == "abandoned"


def test_the_pile_still_takes_work_after_a_run_is_abandoned(pile):
    """The point of ending a run is to be able to move on.

    Note what re-reading the same corpus does: nothing. Those documents were
    genuinely ingested and classified by the abandoned run, and abandoning it
    does not un-read them -- which is the same content-addressed no-op that
    makes a re-sent document harmless everywhere else in this system. So the
    proof that the pile is not wedged is a run that goes all the way through and
    honestly reports that there was nothing new, followed by an actual new
    document that does produce work.
    """
    first = _stopped(pile)
    ops.abandon(first, abandoned_by="vinayak", reason="starting over")

    again = ops.start_run(pile, "pile_acme")
    assert again["status"] == "no_change"

    arrival = ops.arrival(pile, "arrivals/amendment_02.md")
    assert arrival["status"] == "awaiting_approval"
    assert arrival["pending_proposals"] > 0

    runs = {str(r["id"]): r["status"] for r in ops.list_runs(pile)["runs"]}
    assert runs[first] == "abandoned"
    assert runs[arrival["run_id"]] == "awaiting_approval"


def test_the_refusal_is_a_conflict_and_not_a_bad_request(pile):
    """`Finished` is its own type so that a surface can tell the difference
    without reading the message. An ended run reported as a bad request tells
    the caller they made a mistake, and sends them looking for one."""
    run_id = _stopped(pile)
    ops.abandon(run_id, abandoned_by="vinayak", reason="nothing wrong with the request")

    with pytest.raises(ops.Finished):
        ops.resume(run_id)
    # Still an Invalid, so anything already catching refusals keeps working.
    assert issubclass(ops.Finished, ops.Invalid)
