"""Ingest: bytes in, documents and pages out.

Two properties this stage owes the rest of the system:

  Idempotence. Re-ingesting identical bytes into the same pile changes nothing.
  Not "skips the file" -- changes nothing, so a re-run that happens to see the
  same document again cannot produce a second copy or a spurious update.

  Honest gaps. A file we cannot read is recorded with status 'unsupported' and
  a note. It is never silently dropped, because a pile that quietly lost a
  document produces a register that is confidently wrong.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psycopg

from app.ingest.formats import SUPPORTED, ExtractedPage, UnsupportedFormat, detect_format, extract_pages
from app.store.engine import fetch_one


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class IngestResult:
    document_id: str | None
    filename: str
    sha256: str
    format: str | None
    status: str            # ingested | unsupported | empty
    duplicate: bool        # True when these exact bytes were already in the pile
    pages: int = 0
    note: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "ingested" and not self.duplicate


def ingest_bytes(conn: psycopg.Connection, pile_id: str, filename: str, data: bytes,
                 uri: str | None = None) -> IngestResult:
    digest = sha256_bytes(data)
    path = Path(filename)

    existing = fetch_one(
        conn,
        "SELECT id, format, status FROM document WHERE pile_id = %s AND content_sha256 = %s",
        (pile_id, digest),
    )
    if existing:
        # Identical bytes already in this pile. Nothing to do, and saying so is
        # the whole point -- this is what makes a re-run cost nothing.
        return IngestResult(
            document_id=str(existing["id"]), filename=filename, sha256=digest,
            format=existing["format"], status=existing["status"], duplicate=True,
            note="identical bytes already ingested; no change",
        )

    try:
        fmt = detect_format(path, data)
    except UnsupportedFormat as exc:
        doc_id = _insert_document(conn, pile_id, uri or filename, filename, digest,
                                  len(data), "unknown", "unsupported", str(exc))
        return IngestResult(doc_id, filename, digest, None, "unsupported", False, note=str(exc))

    try:
        pages: list[ExtractedPage] = extract_pages(data, fmt)
    except Exception as exc:  # a corrupt PDF is a gap, not a crash
        note = f"could not extract text: {type(exc).__name__}: {exc}"
        doc_id = _insert_document(conn, pile_id, uri or filename, filename, digest,
                                  len(data), fmt, "unsupported", note)
        return IngestResult(doc_id, filename, digest, fmt, "unsupported", False, note=note)

    if not pages:
        note = "no extractable text"
        doc_id = _insert_document(conn, pile_id, uri or filename, filename, digest,
                                  len(data), fmt, "empty", note)
        return IngestResult(doc_id, filename, digest, fmt, "empty", False, note=note)

    doc_id = _insert_document(conn, pile_id, uri or filename, filename, digest,
                              len(data), fmt, "ingested", None)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO page (document_id, page_no, text) VALUES (%s, %s, %s)",
            [(doc_id, p.page_no, p.text) for p in pages],
        )
    return IngestResult(doc_id, filename, digest, fmt, "ingested", False, pages=len(pages))


def _insert_document(conn: psycopg.Connection, pile_id: str, uri: str, filename: str,
                     digest: str, size: int, fmt: str, status: str,
                     note: str | None) -> str:
    row = fetch_one(
        conn,
        """
        INSERT INTO document (pile_id, uri, filename, content_sha256, byte_size,
                              format, status, ingest_note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (pile_id, content_sha256) DO NOTHING
        RETURNING id
        """,
        (pile_id, uri, filename, digest, size, fmt, status, note),
    )
    if row:
        return str(row["id"])
    # Lost a race with a concurrent ingest of the same bytes. The other writer
    # won; its row is the right one. Two runs at once stay two runs.
    row = fetch_one(
        conn,
        "SELECT id FROM document WHERE pile_id = %s AND content_sha256 = %s",
        (pile_id, digest),
    )
    assert row is not None, "unique conflict but no row: schema drift"
    return str(row["id"])


def ingest_path(conn: psycopg.Connection, pile_id: str, path: Path) -> IngestResult:
    return ingest_bytes(conn, pile_id, path.name, path.read_bytes(), uri=str(path))


def ingest_directory(conn: psycopg.Connection, pile_id: str, directory: Path,
                     patterns: Iterable[str] = ("*",)) -> list[IngestResult]:
    """Ingest every file in a directory, in a stable order.

    Sorted so that two runs over the same directory see documents in the same
    sequence -- reproducibility matters more here than speed.
    """
    seen: set[Path] = set()
    for pattern in patterns:
        seen.update(p for p in directory.glob(pattern) if p.is_file())
    return [ingest_path(conn, pile_id, p) for p in sorted(seen)]


def ensure_pile(conn: psycopg.Connection, name: str, domain: str) -> str:
    row = fetch_one(conn, "SELECT id FROM pile WHERE name = %s", (name,))
    if row:
        return str(row["id"])
    row = fetch_one(
        conn,
        "INSERT INTO pile (name, domain) VALUES (%s, %s) RETURNING id",
        (name, domain),
    )
    return str(row["id"])


__all__ = ["ingest_bytes", "ingest_path", "ingest_directory", "ensure_pile",
           "IngestResult", "sha256_bytes", "SUPPORTED"]
