from __future__ import annotations

import uuid
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
    """A connection for reading and for direct writes, always rolled back.

    Rollback is still the right isolation for anything this connection does on
    its own. It is *not* isolation for a run: a run owns its own connection and
    its checkpointer commits it, which is the entire point of behaviour 2. What
    keeps runs from leaking into each other is the pile fixture below.
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


@pytest.fixture(scope="session")
def _test_piles(db_available):
    """Every pile the session created, dropped once at the end.

    Deliberately not a per-test teardown. A test's own connection may still be
    holding rows in an open transaction when its pile goes out of scope, and
    `DELETE FROM pile` would then block on the cascade until that connection
    closes -- which happens after the fixture, so it never does. Deferring the
    drop to the end of the session removes the ordering problem instead of
    trying to win it.
    """
    created: list[str] = []
    yield created
    if not db_available or not created:
        return

    import psycopg
    from psycopg.rows import dict_row

    from app.settings import settings

    with psycopg.connect(settings.database_url, row_factory=dict_row) as teardown:
        with teardown.cursor() as cur:
            # Checkpoints are keyed by run id and have no foreign key to the
            # pile, so they are the one thing the cascade cannot reach.
            cur.execute("SELECT id::text AS id FROM run WHERE pile_id = ANY(%s::uuid[])",
                        (created,))
            run_ids = [row["id"] for row in cur.fetchall()]
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cur.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (run_ids,))
            cur.execute("DELETE FROM pile WHERE id = ANY(%s::uuid[])", (created,))
        teardown.commit()


@pytest.fixture
def pile(db_available, _test_piles):
    """A pile of its own, committed.

    Committed because a run reads it on a different connection. Uniquely named
    because two tests sharing a pile would share its documents, and re-ingesting
    identical bytes is a deliberate no-op -- the second test would silently get
    a run with nothing to do, and pass for the wrong reason.
    """
    if not db_available:
        pytest.skip("Postgres not reachable; start it with `docker compose up -d db`")
    import psycopg
    from psycopg.rows import dict_row

    from app.ingest.ingest import ensure_pile
    from app.settings import settings

    name = f"test-pile-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(settings.database_url, row_factory=dict_row) as setup:
        pile_id = ensure_pile(setup, name, "vendor_contracts")
        setup.commit()
    _test_piles.append(pile_id)
    return pile_id
