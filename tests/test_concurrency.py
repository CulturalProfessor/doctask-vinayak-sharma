"""Two runs at once stay two runs.

Graded behaviour 9. The failure it prevents is not mainly a crash -- it is a
reviewer being handed the same decision twice. Removing the lock and running
two real processes at one pile produces fifteen proposals where there are nine,
with nothing erroring and no sign in the output that anything went wrong. The
loud failures are easier to find and less dangerous than that one.

What this must never do:

  1. Never let two runs write the same pile at the same time.
  2. Never let a refused run leave a trace of a run that never happened.
  3. Never let a run on one pile block a run on another. A lock that serialises
     the whole system is not concurrency safety, it is an outage.
  4. Never hold the pile across the human gate. A reviewer may take a week.
  5. Never leave a pile locked by a process that died holding it.

Contention here is real: two child processes, one of which is made to stall
inside a node so that the race happens on purpose rather than by luck.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from app.store.engine import fetch_all, fetch_one
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.db

STALL_SECONDS = 6.0


def _spawn(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.crashing_run", *args],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _finish(process: subprocess.Popen, timeout: float = 120) -> dict:
    out, err = process.communicate(timeout=timeout)
    assert process.returncode == 0, f"child failed:\n{err[-3000:]}"
    return json.loads(out.strip().splitlines()[-1])


def _await_stall(process: subprocess.Popen, timeout: float = 60) -> None:
    """Block until the stalling child is actually inside the node.

    Reading its announcement instead of sleeping a guessed interval: a test that
    races the thing it is testing passes and fails for reasons that have nothing
    to do with the code.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        if json.loads(line).get("event") == "stalling":
            return
    raise AssertionError("the first run never reached its stall point")


@pytest.fixture
def contended(make_pile):
    """One pile, one run holding it, and a second run trying to take it."""
    pile = make_pile()
    holder = _spawn("--pile", pile, "--stall-at-persist", "1",
                    "--stall-seconds", str(STALL_SECONDS))
    try:
        _await_stall(holder)
        yield pile, holder
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate()


def test_a_second_run_on_a_held_pile_is_turned_away(contended):
    """Refused with a reason, not blocked and not allowed through."""
    pile, holder = contended
    second = _finish(_spawn("--pile", pile, "--wait-seconds", "0.5"))

    assert second["refused"] == "pile_busy"
    assert "is being worked on by run" in second["detail"]
    assert "nothing has been written" in second["detail"]

    _finish(holder, timeout=STALL_SECONDS + 120)


def test_a_refused_run_leaves_no_trace(conn, contended):
    """The pile is taken before the run row is written, so a run that never
    started is not recorded as one. A `running` row that will never move is a
    lie to anyone reading the run list afterwards."""
    pile, holder = contended
    before = fetch_one(conn, "SELECT count(*) AS n FROM run WHERE pile_id = %s",
                       (pile,))["n"]
    _finish(_spawn("--pile", pile, "--wait-seconds", "0.5"))
    conn.rollback()  # see the other connections' commits, not a stale snapshot
    after = fetch_one(conn, "SELECT count(*) AS n FROM run WHERE pile_id = %s",
                      (pile,))["n"]

    assert after == before == 1
    _finish(holder, timeout=STALL_SECONDS + 120)


def test_the_pile_is_not_doubled(conn, contended):
    """The whole point. Forty-eight facts, one of each term, one set of items in
    front of the reviewer."""
    pile, holder = contended
    _finish(_spawn("--pile", pile, "--wait-seconds", "0.5"))
    first = _finish(holder, timeout=STALL_SECONDS + 120)

    conn.rollback()
    assert first["facts"] == 48
    assert fetch_one(conn, "SELECT count(*) AS n FROM fact WHERE pile_id = %s",
                     (pile,))["n"] == 48
    assert fetch_one(conn, "SELECT count(*) AS n FROM document WHERE pile_id = %s",
                     (pile,))["n"] == 7
    assert fetch_one(conn, "SELECT count(*) AS n FROM proposal WHERE pile_id = %s",
                     (pile,))["n"] == 9


def test_a_run_on_another_pile_is_not_blocked(conn, make_pile, contended):
    """A lock that serialises every pile is not safety, it is an outage. This is
    the test that keeps the lock scoped to what it actually protects."""
    _, holder = contended
    other = make_pile()

    started = time.monotonic()
    result = _finish(_spawn("--pile", other, "--wait-seconds", "0.5"))
    elapsed = time.monotonic() - started

    assert result["status"] == "awaiting_approval"
    assert result["facts"] == 48
    assert elapsed < STALL_SECONDS, (
        f"a run on an unrelated pile waited {elapsed:.1f}s for a lock it should "
        f"never have contended for"
    )
    _finish(holder, timeout=STALL_SECONDS + 120)


def test_a_run_waiting_at_the_gate_does_not_hold_the_pile(conn, make_pile):
    """A reviewer may take a week. A gate that holds the pile for that long is a
    global stop button, so the run gives the pile back when it halts and takes
    it again when it is resumed to commit."""
    pile = make_pile()
    first = _finish(_spawn("--pile", pile))
    assert first["status"] == "awaiting_approval"

    # The pile is free while the run waits: a second run can take it, find
    # nothing new to do, and say so.
    second = _finish(_spawn("--pile", pile, "--wait-seconds", "0.5"))
    assert "refused" not in second, "the open gate held the pile"
    assert second["status"] == "no_change"

    conn.rollback()
    duplicated = fetch_all(conn, """
        SELECT kind, summary, count(*) AS n FROM proposal WHERE pile_id = %s
        GROUP BY kind, summary HAVING count(*) > 1
    """, (pile,))
    assert duplicated == [], "the second run put the same items up again"


def test_losing_an_ingest_race_reports_a_duplicate_rather_than_crashing(pile):
    """Below the lock, and it has to work without it.

    The pile lock covers runs. It does not cover `POST /piles/{id}/documents`,
    where two clients can upload identical bytes at the same moment. The loser
    of that race must reach the same conclusion the fast path reaches -- these
    bytes are already in the pile -- rather than inserting pages for the
    winner's document.
    """
    import threading

    import psycopg
    from psycopg.rows import dict_row

    from app.ingest.ingest import ingest_bytes
    from app.settings import settings

    data = b"# Notice\n\nThis pile already holds these exact bytes.\n"
    outcome: dict[str, object] = {}

    first = psycopg.connect(settings.database_url, row_factory=dict_row)
    second = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        # The winner inserts and holds its transaction open. The loser's insert
        # then blocks on the unique index rather than seeing anything, which is
        # the actual race -- a version of this test that lets the first commit
        # first would take the ordinary "already ingested" path and prove
        # nothing.
        winner = ingest_bytes(first, pile, "notice.md", data)

        def losing_ingest() -> None:
            try:
                outcome["result"] = ingest_bytes(second, pile, "notice.md", data)
                second.commit()
            except Exception as exc:  # noqa: BLE001 -- the failure is the finding
                outcome["error"] = exc

        blocked = threading.Thread(target=losing_ingest)
        blocked.start()
        time.sleep(0.3)  # long enough to be certain it is waiting on the index
        assert blocked.is_alive(), "the second insert did not contend at all"
        first.commit()
        blocked.join(timeout=30)

        assert "error" not in outcome, f"the loser crashed: {outcome.get('error')!r}"
        loser = outcome["result"]
        assert winner.duplicate is False and winner.pages == 1
        assert loser.duplicate is True, "the loser reported a fresh ingest"
        assert loser.document_id == winner.document_id

        with second.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM page WHERE document_id = %s",
                        (winner.document_id,))
            assert cur.fetchone()["n"] == 1, "the document's text was written twice"
    finally:
        first.close()
        second.close()


def test_a_process_that_dies_holding_the_pile_releases_it(conn, make_pile):
    """Behaviours 2 and 9 have to hold together. A killed run that kept the pile
    would lock it against the very process sent to resume it, which turns a
    recoverable crash into a stuck pile."""
    pile = make_pile()
    killed = subprocess.run(
        [sys.executable, "-m", "tests.crashing_run", "--pile", pile,
         "--die-at-persist", "2"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert killed.returncode == -9

    conn.rollback()
    run_id = fetch_one(conn, """
        SELECT id::text AS id FROM run WHERE pile_id = %s
        ORDER BY started_at DESC LIMIT 1
    """, (pile,))["id"]

    # No waiting: the lock went when the socket did.
    resumed = _finish(_spawn("--pile", pile, "--resume", run_id,
                             "--wait-seconds", "0"))
    assert resumed["status"] == "awaiting_approval"
    assert resumed["facts"] == 48
