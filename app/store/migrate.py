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
    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"migrations up to date ({len(done)} applied this run)")
    sys.exit(0)
