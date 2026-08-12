"""Where the run's position is kept, and why it is kept atomically.

Graded behaviour 2: `kill -9` mid-run, start again, it continues from where it
stopped. No finished work is redone, no finished work is lost.

The hard half of that sentence is the *and*. Checkpointing on its own gives you
neither guarantee, because the checkpoint and the work are two writes:

  Work committed, checkpoint not     → resume replays the node. Facts inserted
                                       twice, stage events counted twice, the
                                       cost report lies. Work redone.
  Checkpoint committed, work not     → resume skips a node whose output never
                                       landed. Facts missing from a register
                                       that claims to be complete. Work lost.

Both failures are silent and both produce a run that reports success. That is
precisely what behaviour 5 forbids.

So there is exactly one write. `DurableSaver` shares the run's connection and
commits it when LangGraph persists the checkpoint, which puts the node's
database work and the record that the node finished inside the same Postgres
transaction. Either both land or neither does, and a resumed run therefore
starts from a boundary that actually existed.

What this costs: the run holds one connection open with a transaction in flight
between checkpoints. That is fine here -- steps are short, and the per-pile
advisory lock (behaviour 9) needs a session-scoped connection anyway.

`setup()` cannot run inside a transaction (it builds indexes concurrently), so
it gets its own autocommit connection and runs with the schema migrations.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from app.settings import settings


class DurableSaver(PostgresSaver):
    """A PostgresSaver that commits the connection it shares with the run.

    Overriding both write paths is deliberate: `put_writes` is what lands after
    a node returns (including the writes an `interrupt()` leaves behind), and
    `put` is what lands when the loop advances. A crash between them must not
    be able to leave the work committed without the position, or the reverse.
    """

    def put(self, *args: Any, **kwargs: Any) -> Any:
        result = super().put(*args, **kwargs)
        self.conn.commit()
        return result

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        result = super().put_writes(*args, **kwargs)
        self.conn.commit()
        return result


def setup_checkpointer() -> None:
    """Create the checkpoint tables. Runs with the schema migrations.

    Separate connection, autocommit: `CREATE INDEX CONCURRENTLY` refuses to run
    inside a transaction block, and the run's connection always has one open.
    """
    with psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True) as conn:
        PostgresSaver(conn).setup()


@contextmanager
def run_connection() -> Iterator[psycopg.Connection]:
    """The connection a single run owns from start to finish.

    Not autocommit: the saver decides when a transaction closes, and that
    decision is the whole guarantee above.
    """
    conn = psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        # No commit here on purpose. Anything still uncommitted at this point
        # belongs to a step that did not finish, and a step that did not finish
        # has not happened.
        try:
            conn.rollback()
        finally:
            conn.close()


def thread_config(run_id: str) -> dict:
    """A run is a thread. The run id is the only handle resume needs."""
    return {"configurable": {"thread_id": run_id}}


def has_checkpoint(saver: PostgresSaver, run_id: str) -> bool:
    return saver.get_tuple(thread_config(run_id)) is not None
