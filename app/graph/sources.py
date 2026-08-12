"""Reading a document a run is part-way through, and refusing to guess.

A run does not carry document text in its state. It carries the path, and the
nodes that need the words read the file again. That is deliberate -- a
checkpoint holding every page of every document would be enormous, and the state
is written to Postgres on every step -- but it means the source of truth for a
half-finished run lives outside the run, on a disk that anybody can change.

Two things can be different when a resumed run comes back to a file:

  **It is gone.** The file was moved, cleaned up, or the container was pointed at
  a different mount. Nothing can be done about that here, but the failure has to
  say which file and where it was expected, because the fix is to put it back.
  Before this module the run died on a bare FileNotFoundError inside a graph
  node, which reached a reviewer as an HTTP 500 and a JSON parse error in the
  browser -- an unreadable report of a completely readable problem.

  **It is different.** Someone edited or replaced the document while the run was
  halted at the gate. This is the dangerous one and it used to pass silently.
  Every fact this system produces cites a character range in a specific
  document, and those ranges are offsets into the bytes that were ingested. Read
  new bytes at the same path and the offsets still resolve, still look precise,
  and quote text that was never there. A register full of citations that point
  at the wrong words is worse than no register, because it survives review: the
  reviewer checks a quote, the quote is right there, and it is wrong.

So the bytes are checked against the hash recorded at ingest, and a mismatch
refuses. The refusal is recoverable in the way the reviewer would want anyway --
the changed file is a new document, and sending it in as an arrival is exactly
how a change to a contract is supposed to enter this system.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from app.ingest.ingest import sha256_bytes
from app.store.engine import fetch_one


class SourceUnavailable(FileNotFoundError):
    """The document a run is part-way through can no longer be read as ingested.

    A `FileNotFoundError` subclass so that anything already catching that keeps
    working, and its own type so a surface can report it as the recoverable
    situation it is rather than as a crash.
    """


def read_source(conn: psycopg.Connection, path: Path, document_id: str | None) -> bytes:
    """The bytes this run ingested at `path`, or a refusal that says why not.

    `document_id` may be None for a document that never got a row -- an
    unsupported format, or a lost ingest race. There is nothing to compare
    against then, so existence is all that is checked. That is a weaker
    guarantee, and it is the correct one: inventing a hash to check against
    would be worse than admitting there is none.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise SourceUnavailable(
            f"{path.name} is no longer at {path}. This run read it earlier and "
            f"needs it again to continue. Put the file back and resume, or "
            f"start a new reading of the pile."
        ) from None
    except OSError as exc:
        raise SourceUnavailable(f"{path.name} at {path} could not be read: {exc}") from None

    if document_id is None:
        return data

    row = fetch_one(conn, "SELECT content_sha256 FROM document WHERE id = %s", (document_id,))
    if row and row["content_sha256"] != sha256_bytes(data):
        raise SourceUnavailable(
            f"{path.name} has changed on disk since this run read it. Its facts "
            f"cite character positions in the earlier version, so continuing "
            f"would attach quotations to text that no longer says them. Send "
            f"the new version in as an arrival instead; that is a change to the "
            f"pile and belongs in front of a reviewer."
        )
    return data
