"""`kill -9` mid-run, start again, it continues from where it stopped.

Graded behaviour 2, and one of the five that cannot be cut. The brief's sentence
has two halves and the *and* between them is the hard part: **no finished work
is redone, no finished work is lost.** A system can satisfy either half alone by
being careless in the opposite direction, and both failures are silent.

What this must never do, written down before the code that makes it true:

  1. Never lose work a stage finished. A fact persisted and committed before the
     kill is still there afterwards.
  2. Never redo a model call the run already paid for. That is the only work
     here where "redone" costs anything real.
  3. Never double-count. No duplicate facts, no duplicate stage events -- a
     resumed run reporting twice the tokens is behaviour 10 broken by behaviour
     2, and it lies in the expensive direction.
  4. Never commit anything because of a crash. A run killed before the gate has
     written no deliverable at all.
  5. Never produce a different answer than an uninterrupted run would have. The
     register comes out byte-identical, or resumption is not resumption.
  6. Never report a successful resume of a run that has no checkpoint.

Every kill below is a real SIGKILL that a real child process sends to itself.
Nothing runs in that process afterwards: no cleanup, no rollback, no final
flush. Any weaker version gives the system a chance to tidy up on the way out,
which is precisely what a killed process does not get.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.store.engine import fetch_all, fetch_one
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.db

# One classify and one extract for each of the seven documents in pile_acme.
UNINTERRUPTED_MODEL_CALLS = 14


def _child(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tests.crashing_run", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _payload(process: subprocess.CompletedProcess) -> dict:
    assert process.returncode == 0, f"child failed:\n{process.stderr[-3000:]}"
    return json.loads(process.stdout.strip().splitlines()[-1])


def _killed(process: subprocess.CompletedProcess) -> None:
    """Nine, not zero and not one. A child that exited cleanly, or raised, did
    not test what this file claims to test."""
    assert process.returncode == -9, (
        f"expected SIGKILL, got returncode {process.returncode}\n" f"{process.stderr[-3000:]}"
    )


def _run_id(conn, pile: str) -> str:
    row = fetch_one(
        conn,
        """
        SELECT id::text AS id FROM run WHERE pile_id = %s
        ORDER BY started_at DESC LIMIT 1
    """,
        (pile,),
    )
    assert row, "the killed process left no run row at all"
    return row["id"]


@pytest.fixture
def control(make_pile) -> dict:
    """One uninterrupted run, for the killed one to be measured against."""
    return _payload(_child("--pile", make_pile()))


# ------------------------------------------------- killed inside extraction --


@pytest.fixture
def killed_at_persist(conn, make_pile) -> tuple[str, dict]:
    """Dead inside the third extraction, after its model call was recorded.

    The interesting kill point: the answer had been bought and written to the
    ledger, and the work that asked for it had not committed. Everything before
    it is finished work that must survive.
    """
    pile = make_pile()
    _killed(_child("--pile", pile, "--die-at-persist", "3"))
    run_id = _run_id(conn, pile)
    return pile, {"run_id": run_id}


def test_the_kill_leaves_finished_work_behind(conn, killed_at_persist):
    """Not an empty database. Two documents were fully understood before the
    kill and their facts are committed -- if they were not, the resumed run
    would have to redo them and 'no work redone' would be free to claim."""
    pile, _ = killed_at_persist
    facts = fetch_one(conn, "SELECT count(*) AS n FROM fact WHERE pile_id = %s", (pile,))["n"]
    assert 0 < facts < 48, f"expected a partial pile, got {facts} facts"


def test_the_kill_commits_no_deliverable(conn, killed_at_persist):
    """A process killed before the gate has written nothing to approve away."""
    pile, _ = killed_at_persist
    assert (
        fetch_one(conn, "SELECT count(*) AS n FROM deliverable WHERE pile_id = %s", (pile,))["n"]
        == 0
    )
    assert fetch_one(conn, "SELECT count(*) AS n FROM audit WHERE pile_id = %s", (pile,))["n"] == 0


def test_a_second_process_finishes_the_run(conn, control, killed_at_persist):
    """The whole behaviour in one assertion: same pile, same register, byte for
    byte, reached by two processes instead of one."""
    pile, killed = killed_at_persist
    resumed = _payload(_child("--pile", pile, "--resume", killed["run_id"]))

    assert resumed["run_id"] == killed["run_id"], "resuming must not start a new run"
    assert resumed["status"] == "awaiting_approval"
    assert resumed["hashes"] == control["hashes"]


def test_the_resumed_run_does_not_buy_an_answer_twice(conn, control, killed_at_persist):
    """The model call the dead process made is replayed from the run's ledger,
    not re-issued. Across both processes the run pays for exactly what one
    uninterrupted run pays for."""
    pile, killed = killed_at_persist
    resumed = _payload(_child("--pile", pile, "--resume", killed["run_id"]))

    assert control["issued"] == UNINTERRUPTED_MODEL_CALLS
    assert resumed["replayed"] >= 1, "nothing was replayed; the ledger did nothing"

    issued_before_the_kill = fetch_one(
        conn,
        """
        SELECT count(*) AS n FROM model_call WHERE run_id = %s
    """,
        (killed["run_id"],),
    )["n"]
    assert issued_before_the_kill == UNINTERRUPTED_MODEL_CALLS, (
        "the run should have made each of its calls exactly once, across both " "processes"
    )


def test_the_resumed_run_counts_nothing_twice(conn, control, killed_at_persist):
    """A resumed run that double-counts reports a pile that cost more than it
    did. The node in flight when the process died wrote a stage event and
    persisted facts; neither may appear twice."""
    pile, killed = killed_at_persist
    _payload(_child("--pile", pile, "--resume", killed["run_id"]))

    assert fetch_one(conn, "SELECT count(*) AS n FROM fact WHERE pile_id = %s", (pile,))["n"] == 48

    duplicated = fetch_all(
        conn,
        """
        SELECT stage, document_id, count(*) AS n
        FROM stage_event
        WHERE run_id = %s AND stage IN ('classify', 'extract', 'resolve_entity')
        GROUP BY stage, document_id HAVING count(*) > 1
    """,
        (killed["run_id"],),
    )
    assert duplicated == [], f"stages recorded more than once: {duplicated}"

    tokens = fetch_one(
        conn,
        """
        SELECT coalesce(sum(tokens_in), 0) AS n FROM stage_event WHERE run_id = %s
    """,
        (killed["run_id"],),
    )["n"]
    ledger = fetch_one(
        conn,
        """
        SELECT coalesce(sum(tokens_in), 0) AS n FROM model_call WHERE run_id = %s
    """,
        (killed["run_id"],),
    )["n"]
    assert (
        tokens == ledger
    ), "what the run reports spending and what it actually bought have drifted"


def test_the_resumed_run_reviews_each_item_once(conn, control, killed_at_persist):
    """Nine proposals, not eighteen. A reviewer who is handed the same decision
    twice is not being asked a question, they are being worn down."""
    pile, killed = killed_at_persist
    resumed = _payload(_child("--pile", pile, "--resume", killed["run_id"]))
    assert resumed["proposals"] == control["proposals"]

    duplicated = fetch_all(
        conn,
        """
        SELECT kind, summary, count(*) AS n FROM proposal WHERE pile_id = %s
        GROUP BY kind, summary HAVING count(*) > 1
    """,
        (pile,),
    )
    assert duplicated == [], f"the same item was proposed twice: {duplicated}"


# --------------------------------------------- killed before a model call --


def test_a_kill_before_a_call_costs_that_call_and_no_others(conn, control, make_pile):
    """The other kill point. Nothing was bought, so the resumed run buys it --
    once. A run that redoes the calls it already made would show up here as a
    ledger larger than fourteen."""
    pile = make_pile()
    _killed(_child("--pile", pile, "--die-at-model-call", "6"))
    run_id = _run_id(conn, pile)

    assert (
        fetch_one(conn, "SELECT count(*) AS n FROM model_call WHERE run_id = %s", (run_id,))["n"]
        == 5
    ), "five calls made, the sixth never happened"

    resumed = _payload(_child("--pile", pile, "--resume", run_id))
    assert resumed["hashes"] == control["hashes"]
    assert (
        fetch_one(conn, "SELECT count(*) AS n FROM model_call WHERE run_id = %s", (run_id,))["n"]
        == UNINTERRUPTED_MODEL_CALLS
    )


# ------------------------------------------------------ killed at the gate --


def test_the_register_survives_the_process_that_composed_it(conn, make_pile):
    """The gate is where a run waits longest, so it is where a restart is most
    likely. The register that a person approves must not live in the memory of
    the process that built it -- committing is resuming, and the process that
    commits has never seen the register before."""
    from app.stages import gate as gate_module
    from app.stages.gate import Decision
    from app.store import repository as repo

    pile = make_pile()
    first = _payload(_child("--pile", pile))
    assert first["status"] == "awaiting_approval"

    for proposal in repo.list_proposals(conn, first["run_id"], status="pending"):
        gate_module.decide(conn, first["run_id"], [Decision(str(proposal["id"]), True)], "test")
    conn.commit()

    committed = _payload(_child("--pile", pile, "--resume", first["run_id"]))
    assert committed["status"] == "committed"
    assert committed["committed"]["sections_written"] == 6

    stored = {
        row["section_key"]: row["content_hash"] for row in repo.sections_for_version(conn, pile)
    }
    assert (
        stored == first["hashes"]
    ), "the committed register differs from the one that was reviewed"


# ------------------------------------------------------- honest refusals --


def test_resuming_a_run_that_never_checkpointed_refuses(conn, pile):
    """Silence here would look like a successful resume of a run that has no
    position to resume from."""
    from app.graph import pipeline
    from app.llm.fake import FakeProvider
    from app.store import repository as repo

    run_id = repo.create_run(conn, pile, kind="full")
    conn.commit()
    with pytest.raises(LookupError, match="no checkpoint"):
        pipeline.resume(FakeProvider(), run_id=run_id)


def test_resuming_a_run_that_does_not_exist_refuses():
    from app.graph import pipeline
    from app.llm.fake import FakeProvider

    with pytest.raises(LookupError, match="no run"):
        pipeline.resume(FakeProvider(), run_id="00000000-0000-0000-0000-000000000000")


# ------------------------------------ the world under a halted run changing --
#
# A run does not carry document text in its state; it carries the path and reads
# the file again on resume. So between the gate and the commit, the source of
# truth for a half-finished run sits on a disk that anybody can change. Both
# ways that can go wrong used to be failures of the wrong kind: a deleted file
# was an unhandled OS error escaping a graph node, which reached the browser as
# a JSON parse error, and a *changed* file was not an error at all.


def _ingested(conn, pile, tmp_path, body: bytes) -> tuple:
    """A real document row and the file it was read from."""
    from app.ingest.ingest import ingest_bytes

    path = tmp_path / "amendment_01.md"
    path.write_bytes(body)
    result = ingest_bytes(conn, pile, path.name, body, uri=str(path))
    conn.commit()
    return path, result.document_id


def test_a_source_read_back_unchanged_is_just_the_bytes(conn, pile, tmp_path):
    """The control. Without it, a refusal test passes for a system that refuses
    everything, which would be a worse system than the broken one."""
    from app.graph.sources import read_source

    body = b"# Amendment 01\n\nThe standard hourly rate is USD 135 per hour.\n"
    path, document_id = _ingested(conn, pile, tmp_path, body)

    assert read_source(conn, path, document_id) == body


def test_a_source_that_vanished_says_which_file_and_where(conn, pile, tmp_path):
    """The reviewer's next move is to put the file back, so the refusal has to
    name it. This arrived as `FileNotFoundError` from inside a graph node, was
    served as an HTTP 500, and was displayed as `Unexpected token 'I'`."""
    from app.graph.sources import SourceUnavailable, read_source

    path, document_id = _ingested(conn, pile, tmp_path, b"# Amendment 01\n")
    path.unlink()

    with pytest.raises(SourceUnavailable) as raised:
        read_source(conn, path, document_id)
    assert "amendment_01.md" in str(raised.value)
    assert str(path) in str(raised.value)


def test_a_source_that_changed_under_the_run_refuses(conn, pile, tmp_path):
    """The dangerous one, and it used to pass silently.

    Facts cite character ranges into the bytes that were ingested. New bytes at
    the same path still resolve those ranges, still look precise, and quote text
    that was never there. A register whose citations point at the wrong words
    survives review -- the reviewer checks the quote, the quote is right there,
    and it is wrong -- which is why this refuses rather than warns.
    """
    from app.graph.sources import SourceUnavailable, read_source

    path, document_id = _ingested(
        conn, pile, tmp_path, b"The standard hourly rate is USD 135 per hour.\n"
    )
    path.write_bytes(b"The standard hourly rate is USD 205 per hour.\n")

    with pytest.raises(SourceUnavailable, match="changed on disk"):
        read_source(conn, path, document_id)


def test_a_document_with_no_row_is_read_without_inventing_a_hash(conn, pile, tmp_path):
    """An unsupported format or a lost ingest race leaves no row to compare
    against. Existence is then the only honest check; making one up would be
    worse than admitting there is none."""
    from app.graph.sources import read_source

    path = tmp_path / "no_row.md"
    path.write_bytes(b"never ingested\n")

    assert read_source(conn, path, None) == b"never ingested\n"
