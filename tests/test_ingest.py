"""Ingest owes the rest of the system two things: idempotence, and honest gaps.

These are behaviour tests, not mock tests. They put real bytes through the real
extractor into a real database and check what came out the other side.
"""
from __future__ import annotations

import pytest

from app.ingest.ingest import ingest_bytes, ingest_directory, ingest_path, sha256_bytes
from app.store.engine import fetch_all, fetch_one
from tests.conftest import CORPORA, FIXTURES

pytestmark = pytest.mark.db

ACME = CORPORA / "pile_acme"


def test_ingests_the_acme_pile(conn, pile):
    results = ingest_directory(conn, pile, ACME)
    assert len(results) == 7
    assert all(r.status == "ingested" for r in results), [
        (r.filename, r.status, r.note) for r in results if r.status != "ingested"
    ]
    formats = {r.format for r in results}
    assert formats == {"md", "html", "txt"}


def test_reingesting_identical_bytes_changes_nothing(conn, pile):
    """Not "skips the file" -- changes nothing. A re-run that sees the same
    document again must not produce a second copy or a spurious update."""
    path = ACME / "msa_acme_2026.md"
    first = ingest_path(conn, pile, path)
    assert first.duplicate is False

    before = fetch_all(conn, "SELECT id, ingested_at FROM document WHERE pile_id = %s", (pile,))
    pages_before = fetch_one(conn, "SELECT count(*) AS n FROM page")["n"]

    second = ingest_path(conn, pile, path)
    assert second.duplicate is True
    assert second.document_id == first.document_id

    after = fetch_all(conn, "SELECT id, ingested_at FROM document WHERE pile_id = %s", (pile,))
    assert after == before, "re-ingest mutated the document row"
    assert fetch_one(conn, "SELECT count(*) AS n FROM page")["n"] == pages_before


def test_same_content_under_a_different_filename_is_still_a_duplicate(conn, pile):
    """Content-addressed, not name-addressed. The same invoice mailed twice
    under two names is one document."""
    data = (ACME / "invoice_1043.txt").read_bytes()
    ingest_bytes(conn, pile, "invoice_1043.txt", data)
    again = ingest_bytes(conn, pile, "invoice_1043_COPY.txt", data)
    assert again.duplicate is True


def test_unsupported_format_is_recorded_as_a_gap_not_dropped(conn, pile):
    """A pile that quietly lost a document produces a register that is
    confidently wrong. The gap has to be visible."""
    result = ingest_bytes(conn, pile, "quarterly_costs.xlsx", b"PK\x03\x04 not really")
    assert result.status == "unsupported"
    assert result.duplicate is False
    row = fetch_one(conn, "SELECT status, ingest_note FROM document WHERE id = %s",
                    (result.document_id,))
    assert row["status"] == "unsupported"
    assert "xlsx" in row["ingest_note"]


def test_unreadable_bytes_are_a_gap_not_a_crash(conn, pile):
    result = ingest_bytes(conn, pile, "scan.pdf", b"%PDF-1.7\nthis is not a real pdf")
    assert result.status == "unsupported"
    assert result.note and "could not extract" in result.note


def test_empty_document_is_recorded_as_empty(conn, pile):
    result = ingest_bytes(conn, pile, "blank.txt", b"\n   \n")
    assert result.status == "empty"


def test_pages_carry_the_text_spans_will_cite(conn, pile):
    result = ingest_path(conn, pile, ACME / "sow_014.html")
    pages = fetch_all(conn, "SELECT page_no, text FROM page WHERE document_id = %s",
                      (result.document_id,))
    assert len(pages) == 1
    assert "four hundred (400) hours" in pages[0]["text"]


def test_the_poisoned_document_ingests_as_ordinary_data(conn, pile):
    """A document full of instructions aimed at the system is still just a
    document at this stage. Ingest stores it; the quarantine branch downstream
    is what refuses to obey it. Nothing here may act on its content."""
    data = (FIXTURES / "poisoned_invoice.txt").read_bytes()
    result = ingest_bytes(conn, pile, "poisoned_invoice.txt", data)
    assert result.status == "ingested"
    row = fetch_one(conn, "SELECT status FROM document WHERE id = %s", (result.document_id,))
    assert row["status"] == "ingested"
    page = fetch_one(conn, "SELECT text FROM page WHERE document_id = %s", (result.document_id,))
    assert "Ignore all" in page["text"], "instruction text must be preserved verbatim to report on"


def test_sha256_is_content_addressed():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")
