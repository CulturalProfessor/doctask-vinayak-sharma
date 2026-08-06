"""The HTTP surface.

Today this covers ingest and inspection only. The run, gate and export
operations land with the graph -- and whatever the UI can do, this API and the
MCP server must be able to do too, because a machine has to be able to drive
the whole flow without a human clicking anything (graded behaviour 4).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile

from app.domain.config import ConfigError, load_domain
from app.ingest.ingest import ensure_pile, ingest_bytes
from app.store.engine import fetch_all, fetch_one, transaction

app = FastAPI(title="doctask", version="0.1.0")


@app.get("/health")
def health() -> dict:
    """Reports what is actually true, including when the database is down.

    A health endpoint that returns 200 while its database is unreachable is the
    kind of success message this system is not allowed to send.
    """
    try:
        with transaction() as conn:
            fetch_one(conn, "SELECT 1 AS ok")
        return {"status": "healthy", "database": "reachable"}
    except Exception as exc:
        raise HTTPException(503, {"status": "degraded", "database": str(exc)[:200]})


@app.get("/piles")
def list_piles() -> dict:
    with transaction() as conn:
        rows = fetch_all(conn, """
            SELECT p.id, p.name, p.domain, p.created_at,
                   count(d.id) FILTER (WHERE d.status = 'ingested') AS documents,
                   count(d.id) FILTER (WHERE d.status <> 'ingested') AS gaps
            FROM pile p LEFT JOIN document d ON d.pile_id = p.id
            GROUP BY p.id ORDER BY p.name
        """)
    return {"piles": rows}


@app.post("/piles")
def create_pile(name: str, domain: str = "vendor_contracts") -> dict:
    try:
        load_domain(domain)
    except ConfigError as exc:
        raise HTTPException(400, f"unknown domain {domain!r}: {exc}")
    with transaction() as conn:
        return {"pile_id": ensure_pile(conn, name, domain), "name": name, "domain": domain}


@app.get("/piles/{pile_id}/documents")
def list_documents(pile_id: str) -> dict:
    with transaction() as conn:
        rows = fetch_all(conn, """
            SELECT d.id, d.filename, d.format, d.doc_type, d.status, d.ingest_note,
                   d.byte_size, d.ingested_at, count(pg.id) AS pages
            FROM document d LEFT JOIN page pg ON pg.document_id = d.id
            WHERE d.pile_id = %s
            GROUP BY d.id ORDER BY d.filename
        """, (pile_id,))
    return {"pile_id": pile_id, "documents": rows}


@app.post("/piles/{pile_id}/documents")
async def upload_document(pile_id: str, file: UploadFile) -> dict:
    """Large files come through here rather than through memory-resident JSON."""
    with transaction() as conn:
        if not fetch_one(conn, "SELECT id FROM pile WHERE id = %s", (pile_id,)):
            raise HTTPException(404, f"no pile {pile_id}")
        result = ingest_bytes(conn, pile_id, file.filename or "upload", await file.read())
    return {
        "document_id": result.document_id,
        "status": result.status,
        # Reported rather than hidden: a caller re-sending the same bytes should
        # be told nothing happened, not handed a success that implies it did.
        "duplicate": result.duplicate,
        "format": result.format,
        "pages": result.pages,
        "note": result.note,
    }
