"""One writer per pile.

Graded behaviour 9: two runs at the same time stay two runs. What goes wrong
without it was measured by removing the lock and running two real processes at
one pile, not reasoned about:

  **The same question reaches a reviewer twice.** A second run starting while
  the first is mid-flight proposes the same sections and the same conflicts
  again: fifteen items where there are nine. This is the silent one, and the
  worst, because nothing errors and the reviewer simply gets a longer list.

  **Two overlapping ingests collide.** Both runs insert the same document; the
  unique constraint saves one, and the loser used to go on to insert pages for
  the winner's document and die on `page (document_id, page_no)`. That is a
  defect in ingest rather than in locking -- a lost race was being reported as a
  fresh ingest -- so it is fixed there, where the HTTP upload path needs it too.
  See `app/ingest/ingest.py`.

  **Two versions could claim the same predecessor.** `max(version) + 1` computed
  concurrently gives both runs the same answer, and only the unique constraint
  on `(pile_id, version)` stops it. Loud rather than silent, which is luck.

So a run holds its pile while it is working on it. The lock is session-scoped
and lives on the connection the run owns, which gets two things right that a
transaction-scoped lock does not: it survives the checkpoint commits that now
punctuate every run, and it is released the instant a killed process's socket
closes. Behaviour 2 and behaviour 9 have to hold at the same time, and a dead
run holding a pile against the process sent to resume it would break both.

**The lock is not held across the gate.** A run halts at `interrupt()` and
`_drive` returns, closing the connection and releasing the pile. A reviewer may
take a week; holding a pile for that long would make the gate a global stop
button. The run takes the pile back when it is resumed to commit -- and the
commit-time hash check is what makes that safe, because if the pile moved while
the reviewer was thinking, the approved bytes no longer match and the commit
refuses rather than writing over someone else's work.

Contention is at the pile, not the graph. LangGraph's checkpointing is
per-thread and has nothing to say about two threads on one pile, which is why
this lives in our schema. PLAN.md section 6 called this one correctly.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.store.engine import fetch_one, release_session_lock, try_session_lock


class PileBusy(RuntimeError):
    """Another run is working on this pile.

    Deliberately an error rather than a wait. A caller that is told the pile is
    busy, and which run has it, can decide what to do; a caller left hanging on
    a lock cannot. The waiting that *is* worth doing -- the fraction of a second
    where two triggers genuinely raced -- happens below before this is raised.
    """


@contextmanager
def hold_pile(conn: psycopg.Connection, pile_id: str,
              wait_seconds: float = 2.0) -> Iterator[None]:
    """Hold the pile for the duration of a run's working phase."""
    key = f"pile:{pile_id}"
    deadline = time.monotonic() + max(0.0, wait_seconds)

    while True:
        if try_session_lock(conn, key):
            break
        if time.monotonic() >= deadline:
            raise PileBusy(
                f"pile {pile_id} is being worked on by {_holder(conn, pile_id)}; "
                f"this run has not started and nothing has been written"
            )
        # Short enough that a genuine race resolves without anyone noticing,
        # long enough not to spin.
        time.sleep(0.05)

    try:
        yield
    finally:
        _release(conn, key)


def _release(conn: psycopg.Connection, key: str) -> None:
    """Give the pile back without ever becoming the error anyone sees.

    Two things go wrong here if this is written as a bare unlock, and both were
    found rather than predicted. If the run failed, its transaction is aborted
    and Postgres refuses every further statement -- so the unlock raises
    `InFailedSqlTransaction` from a `finally`, which *replaces* the exception
    that actually killed the run. The real cause is gone and what surfaces is a
    complaint about transaction state.

    So: roll back first, which is safe because a session-scoped advisory lock
    does not belong to a transaction and survives one. Then unlock, and swallow
    anything that still goes wrong -- closing the connection releases the lock
    regardless, so a failure here has nothing left to protect and everything to
    obscure.
    """
    try:
        conn.rollback()
        release_session_lock(conn, key)
    except Exception:
        pass


def _holder(conn: psycopg.Connection, pile_id: str) -> str:
    """Name the run that has the pile, if one can be named.

    Best effort on purpose: the lock is the authority, and this is only here so
    that the refusal says something a person can act on instead of "busy".
    """
    row = fetch_one(conn, """
        SELECT id::text AS id, kind, started_at FROM run
        WHERE pile_id = %s AND status = 'running'
        ORDER BY started_at DESC LIMIT 1
    """, (pile_id,))
    if not row:
        return "another process"
    return f"run {row['id']} ({row['kind']}, started {row['started_at']:%H:%M:%S})"
