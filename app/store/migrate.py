"""Numbered SQL migrations, applied in order, recorded so they run once.

No Alembic. The schema is small enough that plain SQL files are easier to read
than a migration DSL, and a reviewer can see the whole data model in one file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.store.engine import connect

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename     TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def pending(conn) -> list[Path]:
    with conn.cursor() as cur:
        cur.execute(_BOOTSTRAP)
        cur.execute("SELECT filename FROM schema_migration")
        applied = {r["filename"] for r in cur.fetchall()}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]


def migrate() -> list[str]:
    applied: list[str] = []
    with connect() as conn:
        for path in pending(conn):
            with conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute("INSERT INTO schema_migration (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
            print(f"  applied {path.name}")

    # The checkpoint tables are LangGraph's, and it owns their migrations. They
    # go up here anyway so that a fresh clone reaches a resumable system with
    # one command rather than two -- behaviour 6 and behaviour 2 have to arrive
    # together, or the first run after a `docker compose up` is the one that
    # cannot be resumed.
    from app.graph.checkpoint import setup_checkpointer

    setup_checkpointer()
    print("  checkpoint tables up to date")
    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"migrations up to date ({len(done)} applied this run)")
    sys.exit(0)
