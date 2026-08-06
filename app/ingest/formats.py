"""Turning bytes into text that a span can point into.

Provenance is only as good as the offsets, so this module is deliberate about
what granularity each format can honestly support:

  txt, md   exact character offsets into the file's own text
  html      exact offsets into the extracted text, not the markup
  docx      exact offsets into the reconstructed text; one page
  pdf       exact offsets *within a page*, page numbers preserved

PDF text extraction does not give reliable character offsets back into the
original layout, so a PDF span is honest at page granularity plus offsets into
the extracted page text. That limitation is declared in the README rather than
papered over.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUPPORTED = ("txt", "md", "html", "pdf", "docx")

_EXT = {
    ".txt": "txt", ".text": "txt",
    ".md": "md", ".markdown": "md",
    ".html": "html", ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
}


class UnsupportedFormat(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    page_no: int
    text: str


def detect_format(path: Path, data: bytes | None = None) -> str:
    """Extension first, magic bytes as the tiebreaker.

    A .txt file that is really a PDF should be read as a PDF, not ingested as
    binary noise that silently produces garbage facts.
    """
    if data is not None:
        if data[:5] == b"%PDF-":
            return "pdf"
        if data[:2] == b"PK" and path.suffix.lower() == ".docx":
            return "docx"
    fmt = _EXT.get(path.suffix.lower())
    if fmt is None:
        raise UnsupportedFormat(
            f"{path.name}: extension {path.suffix!r} is not one of {SUPPORTED}"
        )
    return fmt


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _html_to_text(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _pdf_pages(data: bytes) -> list[ExtractedPage]:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(ExtractedPage(i, text))
    return pages


def _docx_text(data: bytes) -> str:
    import io

    from docx import Document

    doc = Document(io.BytesIO(data))
    blocks = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            blocks.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(b for b in blocks if b.strip())


def extract_pages(data: bytes, fmt: str) -> list[ExtractedPage]:
    """Extract text as pages. Formats without pagination produce a single page 1."""
    if fmt == "pdf":
        return _pdf_pages(data)
    if fmt == "html":
        text = _html_to_text(data)
    elif fmt == "docx":
        text = _docx_text(data)
    elif fmt in ("txt", "md"):
        text = _decode(data)
    else:
        raise UnsupportedFormat(f"no extractor for format {fmt!r}")
    text = text.strip()
    return [ExtractedPage(1, text)] if text else []
