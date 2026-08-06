from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPORA = REPO_ROOT / "corpora"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def db_available() -> bool:
    try:
        import psycopg

        from app.settings import settings

        with psycopg.connect(settings.database_url, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture
def conn(db_available):
    """A connection wrapped in a transaction that is always rolled back.

    Every db test therefore starts from the same state and leaves no residue,
    which is what lets them run in any order or in parallel.
    """
    if not db_available:
        pytest.skip("Postgres not reachable; start it with `docker compose up -d db`")
    import psycopg
    from psycopg.rows import dict_row

    from app.settings import settings

    connection = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def pile(conn):
    from app.ingest.ingest import ensure_pile

    return ensure_pile(conn, "test-pile", "vendor_contracts")
