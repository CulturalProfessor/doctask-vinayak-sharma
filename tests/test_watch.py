"""The watched location.

What is worth testing here is not "does it see a file" -- it is the four
behaviours that make a watcher safe to leave running: it waits for a file to
finish arriving, it does not dispatch the same bytes twice, it gives way when
another run holds the pile, and it does not retry a file that is deterministically
broken. The dispatch itself is `ops.arrival`, which the incremental tests
already cover end to end; here it is injected, so these tests prove the trigger
rather than re-proving the pipeline.
"""

from __future__ import annotations

import pytest

from app import operations as ops
from app.graph.locking import PileBusy
from app.watch import Watcher, WatchMisconfigured
from tests.conftest import CORPORA


@pytest.fixture
def inbox(tmp_path_factory):
    """A watch directory inside corpora/, because that is the only place the
    watcher is allowed to look -- arrivals resolve under that root."""
    directory = CORPORA / "inbox" / f"test-{tmp_path_factory.mktemp('w').name}"
    directory.mkdir(parents=True, exist_ok=True)
    yield directory
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()


class Recorder:
    """Stands in for `ops.arrival`, and can be told to misbehave."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raises = raises

    def __call__(self, pile_id: str, document: str, domain: str) -> dict:
        self.calls.append((pile_id, document))
        if self.raises is not None:
            raise self.raises
        return {"run_id": None, "status": "awaiting_approval", "note": "1 document read"}


# ------------------------------------------------------------- settling --


def test_a_file_still_being_written_is_not_dispatched(inbox):
    """The reason this polls instead of listening for events.

    A file appears the instant a copy starts and finishes some time later. An
    event-driven watcher reads it half-written; this one requires size and mtime
    to hold still across two scans.
    """
    watcher = Watcher(inbox, "irrelevant", dispatch=Recorder())
    partial = inbox / "msa.md"
    partial.write_text("# Master Services Agreement\n")

    assert watcher.settled_files() == [], "first sight of a file is never settled"

    partial.write_text("# Master Services Agreement\nThe hourly rate is USD 120.\n")
    assert watcher.settled_files() == [], "it changed, so it is still arriving"

    assert [p.name for p in watcher.settled_files()] == ["msa.md"]


def test_partial_and_hidden_files_are_ignored_entirely(inbox):
    watcher = Watcher(inbox, "irrelevant", dispatch=Recorder())
    (inbox / "contract.md.part").write_text("half a file")
    (inbox / ".hidden.md").write_text("editor droppings")
    (inbox / "real.md").write_text("a document")

    watcher.settled_files()
    assert [p.name for p in watcher.settled_files()] == ["real.md"]


def test_a_watch_dir_outside_corpora_is_refused_at_startup(tmp_path):
    """Loud at startup rather than per-file later.

    `arrival` refuses paths outside corpora/, and that check is a boundary worth
    keeping. A watcher pointed somewhere else would start happily and then
    reject every single file, which looks like a broken corpus rather than a
    misconfiguration.
    """
    with pytest.raises(WatchMisconfigured) as exc:
        Watcher(tmp_path, "acme")
    assert "corpora" in str(exc.value)


# ------------------------------------------------------- dispatching, live --


@pytest.mark.db
def test_a_settled_file_dispatches_exactly_once(inbox, pile, conn):
    """Settling is not an event -- a finished file is settled on every scan
    afterwards -- so the thing that stops a second dispatch is the pile's own
    content hash, not the scanner."""
    _name_pile(conn, pile, "watch-test-once")
    recorder = Recorder()
    watcher = Watcher(inbox, "watch-test-once", dispatch=recorder)
    (inbox / "amendment.md").write_text("# Amendment 3\nThe rate becomes USD 140.\n")

    watcher.settled_files()  # first sight
    outcomes = watcher.tick()  # settled, dispatched

    assert [o.outcome for o in outcomes] == ["dispatched"]
    assert len(recorder.calls) == 1
    # The path handed to `arrival` is relative to corpora/, like every other
    # caller's.
    assert recorder.calls[0][1].startswith("inbox/")

    # The file is still sitting there, still settled, on every later tick.
    assert watcher.tick() == []
    assert watcher.tick() == []
    assert len(recorder.calls) == 1


@pytest.mark.db
def test_a_busy_pile_defers_rather_than_failing(inbox, pile, conn):
    """Behaviour 9: two runs at once stay two runs.

    The watcher's job when it loses that race is to wait. Recording a failure
    would permanently refuse a document because of a moment's contention.
    """
    _name_pile(conn, pile, "watch-test-busy")
    busy = Recorder(raises=PileBusy("pile is held by run abc"))
    watcher = Watcher(inbox, "watch-test-busy", dispatch=busy)
    (inbox / "invoice.txt").write_text("Invoice 9001. Amount due USD 4,200.\n")

    watcher.settled_files()
    assert [o.outcome for o in watcher.tick()] == ["busy"]

    # Nothing was recorded, so the next tick tries again.
    assert [o.outcome for o in watcher.tick()] == ["busy"]
    assert len(busy.calls) == 2

    # And once the pile frees up, it lands.
    watcher.dispatch = Recorder()
    assert [o.outcome for o in watcher.tick()] == ["dispatched"]


@pytest.mark.db
def test_a_deterministically_broken_file_is_not_retried_forever(inbox, pile, conn):
    """The one thing that has to be remembered rather than recomputed.

    A file that fails never reaches the document table, so without a `failed`
    row it would be re-attempted on every tick until someone cleaned the inbox
    by hand.
    """
    _name_pile(conn, pile, "watch-test-broken")
    broken = Recorder(raises=ops.Invalid("no document at corpora/inbox/x"))
    watcher = Watcher(inbox, "watch-test-broken", dispatch=broken)
    (inbox / "broken.md").write_text("something the pipeline refuses\n")

    watcher.settled_files()
    outcomes = watcher.tick()
    assert [o.outcome for o in outcomes] == ["failed"]
    assert "Invalid" in (outcomes[0].detail or "")

    assert watcher.tick() == []
    assert len(broken.calls) == 1

    # And the refusal is readable rather than only logged.
    status = ops.watch_status()
    assert status["enabled"] is False  # tests never start the loop


@pytest.mark.db
def test_a_missing_pile_defers_instead_of_failing(inbox):
    """A startup ordering problem must not permanently refuse a document."""
    recorder = Recorder()
    watcher = Watcher(inbox, "no-such-pile-anywhere", dispatch=recorder)
    (inbox / "arrival.md").write_text("# Amendment 4\n")

    watcher.settled_files()
    assert [o.outcome for o in watcher.tick()] == ["no_pile"]
    assert recorder.calls == []

    # Nothing recorded, so it is retried rather than dropped.
    assert [o.outcome for o in watcher.tick()] == ["no_pile"]


@pytest.mark.db
def test_a_crashing_dispatch_does_not_stop_the_watcher(inbox, pile, conn):
    """A watcher that dies on one bad file stops watching, and every document
    after it is lost with no error anywhere."""
    _name_pile(conn, pile, "watch-test-crash")
    watcher = Watcher(inbox, "watch-test-crash", dispatch=Recorder(raises=RuntimeError("boom")))
    (inbox / "one.md").write_text("first\n")
    (inbox / "two.md").write_text("second\n")

    watcher.settled_files()
    outcomes = watcher.tick()
    assert {o.outcome for o in outcomes} == {"failed"}
    assert len(outcomes) == 2, "the second file is still processed"


def _name_pile(conn, pile_id: str, name: str) -> None:
    """Give the test's pile a stable name, since the watcher resolves by name.

    Committed on its own connection: the watcher opens connections of its own
    and would not see an uncommitted rename.
    """
    import psycopg
    from psycopg.rows import dict_row

    from app.settings import settings

    with psycopg.connect(settings.database_url, row_factory=dict_row) as setup:
        with setup.cursor() as cur:
            cur.execute("UPDATE pile SET name = %s WHERE id = %s", (name, pile_id))
        setup.commit()
