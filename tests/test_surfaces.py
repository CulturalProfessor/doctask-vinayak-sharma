"""What the operations layer says about a pile, and whether it is true.

These are the answers every surface displays without re-deriving: how many
documents a pile has, which of them are gaps, and which runs are still holding
work at the gate. They are worth their own tests because the failure mode is not
a crash -- it is a screen that is confidently wrong, which is the specific thing
this system is not allowed to be.

The bug that prompted the first two: `list_piles` counted a document as read
only if its status was still `'ingested'`, and called everything else a gap. So
a pile whose seven documents had all been classified and extracted reported "0
documents, 7 not read" while its register sat there composed from their facts.
Nothing failed; the number was just a lie, in the place a reviewer looks first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import operations as ops
from app.store import repository as repo

pytestmark = pytest.mark.db


def _pile(piles: list[dict], pile_id: str) -> dict:
    # Ids come back as UUID objects; both surfaces stringify them on the way out.
    return next(row for row in piles if str(row["id"]) == pile_id)


def test_a_document_that_was_read_is_never_counted_as_a_gap(conn, pile):
    """Progress through the pipeline is not failure.

    `classified` and `extracted` are states a document reaches by being
    understood. Counting them against the pile made success look like damage.
    """
    ops.start_run(pile, "pile_acme")

    row = _pile(ops.list_piles()["piles"], pile)
    assert row["documents"] == 7, (
        "every document in pile_acme was read; the count must say so whatever "
        "stage each one has since moved through"
    )
    assert row["gaps"] == 0

    conn.rollback()
    statuses = {r["status"] for r in repo.documents_for_pile(conn, pile)}
    assert statuses - {"ingested"}, (
        "this test is only meaningful while the pipeline advances document "
        "status past 'ingested'"
    )


def test_a_gap_is_counted_as_one(conn, pile):
    """The other direction: a quarantined document must not be quietly counted
    as understood. Gaps are output, and a hidden one is worse than a loud one."""
    ops.start_run(pile, "pile_acme")

    conn.rollback()
    document = repo.documents_for_pile(conn, pile)[0]
    conn.execute("UPDATE document SET status = 'quarantined' WHERE id = %s",
                 (document["id"],))
    conn.commit()
    try:
        row = _pile(ops.list_piles()["piles"], pile)
        assert row["documents"] == 6 and row["gaps"] == 1

        listed = ops.list_documents(pile)["documents"]
        assert sum(1 for d in listed if d["is_gap"]) == 1, (
            "a surface must not have to guess which statuses mean 'not read'"
        )
    finally:
        conn.execute("UPDATE document SET status = %s WHERE id = %s",
                     (document["status"], document["id"]))
        conn.commit()


def test_a_pile_whose_bytes_were_seeded_is_still_read(conn, pile):
    """Already stored is not already understood.

    `docker compose up` seeds the demo pile by ingesting its bytes. `ingest`
    then skipped every document as a duplicate, queued none of them, extracted
    nothing, and composed six sections with zero citations and fifteen gaps --
    while reporting "7 documents" and opening the gate as though it had worked.
    The first thing a reviewer saw on a fresh clone was an empty register with a
    successful run behind it.

    This is the shape of failure the whole project is against: not a crash, a
    confident nothing.
    """
    from app.ingest.ingest import ingest_directory
    from tests.conftest import CORPORA

    ingest_directory(conn, pile, CORPORA / "pile_acme")
    conn.commit()

    started = ops.start_run(pile, "pile_acme")
    assert started["duplicates"], "the fixture is pointless unless they were pre-ingested"
    assert started["facts"] > 0, (
        "a document whose bytes happened to be stored already must still be read"
    )
    assert started["conflicts"] == 3


def test_a_document_already_read_is_not_read_twice(conn, pile):
    """The other half of the same rule, and the reason it is keyed on status
    rather than on 'have I seen these bytes in this process'. A second run over
    a pile that is already understood must cost nothing."""
    ops.start_run(pile, "pile_acme")

    again = ops.start_run(pile, "pile_acme")
    assert again["model_calls"] == 0, (
        "re-reading an understood pile would spend money to learn nothing"
    )
    assert again["status"] == "no_change"


def test_a_run_stopped_at_the_gate_can_be_found_again(pile):
    """Behaviour 2 from the other end.

    A run halted for review belongs to the pile, not to whichever client started
    it. Without a way to list it, the only handle on an open gate is a run id
    someone happened to keep -- and losing it would strand the review with no
    way to reach it and no way to know it was there.
    """
    started = ops.start_run(pile, "pile_acme")
    assert started["status"] == "awaiting_approval"

    runs = ops.list_runs(pile)["runs"]
    assert [str(r["id"]) for r in runs] == [started["run_id"]]
    assert runs[0]["pending_proposals"] == started["pending_proposals"] > 0

    waiting = ops.list_runs(pile, status="awaiting_approval")["runs"]
    assert [str(r["id"]) for r in waiting] == [started["run_id"]]
    assert ops.list_runs(pile, status="committed")["runs"] == []

    with pytest.raises(ops.NotFound):
        ops.list_runs("00000000-0000-0000-0000-000000000000")


def test_the_pending_count_falls_as_the_review_happens(pile):
    """The listing has to track the review rather than the run's opening state,
    or a reviewer returning to it would be told there is work waiting that they
    already did."""
    started = ops.start_run(pile, "pile_acme")
    proposals = ops.list_proposals(started["run_id"], "pending")["proposals"]

    ops.decide(started["run_id"],
               [{"proposal_id": proposals[0]["id"], "approved": True}],
               decided_by="vinayak")

    row = ops.list_runs(pile)["runs"][0]
    assert row["pending_proposals"] == len(proposals) - 1
    assert row["proposals"] == len(proposals)


# ------------------------------------------------------- what can be read --

def test_the_corpora_listing_offers_what_is_actually_there():
    """`start_run` takes a folder and `arrival` takes a file path, and until
    this existed the only way to learn either was to already know it.

    Needs no pile: what is on disk is not a property of any pile, and a caller
    choosing what to read has not chosen a pile to read it into yet.
    """
    body = ops.corpora()
    names = {f["name"] for f in body["folders"]}
    assert {"pile_acme", "pile_northwind", "arrivals"} <= names

    acme = next(f for f in body["folders"] if f["name"] == "pile_acme")
    assert acme["files"] == acme["readable"] == 7

    # Every path is exactly what the operation it feeds will accept.
    paths = {f["path"] for f in body["files"]}
    assert "arrivals/amendment_02.md" in paths
    assert all(not p.startswith("/") for p in paths)


def test_a_file_that_cannot_be_read_is_listed_and_marked_rather_than_hidden():
    """A picker that silently omits a document leaves someone hunting for a file
    that is right there. The same rule ingest follows for a format it refuses:
    a gap is output, not something to quietly drop.

    `pile_northwind/rate_card_2026.csv` is the case in this repo -- a real
    document in a real corpus that this system does not read.
    """
    body = ops.corpora()
    csv = next(f for f in body["files"] if f["path"].endswith("rate_card_2026.csv"))
    assert csv["supported"] is False
    assert csv["format"] is None
    assert "csv" in (csv["note"] or ""), "the reason has to travel with the file"

    # And the folder's own count says so, so a caller sees it before opening it.
    northwind = next(f for f in body["folders"] if f["name"] == "pile_northwind")
    assert northwind["readable"] < northwind["files"]


# ----------------------------------------------------------- sent documents --

def test_an_uploaded_document_is_stored_and_actually_read(pile):
    """Uploading and having the document read are one intention, not two.

    The endpoint behind this used to store the bytes and stop, which returned a
    success response for a document that never became a fact, never reached the
    register and never appeared for review, and nothing said so. The claim worth
    testing is that an upload produces a run holding items for a person.

    It sends the bytes of a document the recordings already cover, under its own
    name: a suite that invented a contract here would only prove that the fake
    provider fails loudly, which is a different test and one that already exists.
    """
    from tests.conftest import CORPORA

    source = CORPORA / "arrivals" / "amendment_02.md"
    result = ops.upload(pile, source.name, source.read_bytes())
    stored = ops.CORPORA / result["stored_as"]
    try:
        assert result["stored_as"].startswith("uploads/")
        assert result["bytes"] == source.stat().st_size
        # It went through `arrival`, so it has a run and stopped for a person.
        assert result["run_id"]
        assert result["status"] in ("awaiting_approval", "no_change")
        assert stored.is_file() and stored.read_bytes() == source.read_bytes()
    finally:
        stored.unlink(missing_ok=True)


def test_a_filename_that_climbs_out_of_the_corpus_cannot(tmp_path):
    """One of the surfaces this sits behind is driven by a model reading
    documents that may themselves contain instructions, so a caller-supplied
    filename is never trusted as a path.

    It is neutralised rather than refused: only the last part of the name is
    kept, so `../../etc/passwd` becomes a document called `passwd` inside the
    uploads folder. Refusing outright would be surprising for a legitimately odd
    filename, and the response says exactly where the bytes went either way.
    """
    landed = ops._free_path(ops.UPLOADS / Path("../../etc/passwd").name, b"x")
    assert landed.parent == ops.UPLOADS
    assert ops.UPLOADS in landed.parents or landed.parent == ops.UPLOADS
    assert "etc" not in str(landed.relative_to(ops.CORPORA))


def test_a_name_already_taken_by_different_bytes_is_never_overwritten(tmp_path):
    """A document already in a pile is cited by character offsets into the text
    that was read from it. Replacing the file underneath would leave every
    citation pointing at a document that no longer says what was quoted: the
    register stays internally consistent and stops being checkable, which is the
    worse of the two failures.

    A pure test of the naming rule, with no run behind it, because the rule is
    what has to hold.
    """
    first, second = b"sixty (60) days", b"ninety (90) days"
    target = tmp_path / "clash.md"
    target.write_bytes(first)

    assert ops._free_path(target, first) == target, "same bytes reuse the file"

    moved = ops._free_path(target, second)
    assert moved != target
    assert moved.name.startswith("clash-") and moved.suffix == ".md"
    assert target.read_bytes() == first, "the document already there is untouched"
